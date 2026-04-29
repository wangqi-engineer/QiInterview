"""火山引擎语音 V3 单向流式 TTS 客户端封装（v0.4 重构）。

API: ``POST https://openspeech.bytedance.com/api/v3/tts/unidirectional``

鉴权头：
  - ``X-Api-Key``        = 前端 ``volc_voice_key``（透传，不回退环境变量）
  - ``X-Api-Resource-Id``= ``seed-tts-2.0``（对应 BigTTS 2.0 大模型音色）
  - ``X-Api-Request-Id`` = 随机 UUID（线上排查 trace 用）

请求体（JSON）：
  {
    "user": {"uid": "qi-interview"},
    "req_params": {
      "text": "<整段文字>",
      "speaker": "<音色 ID>",
      "audio_params": {"format":"mp3", "sample_rate":24000}
    }
  }

响应体（NDJSON / SSE-ish）：每行可选 ``data:`` 前缀，JSON 含
  - ``code`` ∈ {0, 20000000}：正常进度
  - ``data``：base64 编码的音频块（mp3/pcm/ogg 字节）
  - ``message`` / 其它非 0 code：错误，整段失败。

与旧版双向流式 ``TtsSession`` 的契约对齐（``voice_ws`` 不需要改方法签名）：
  - ``start()``：标记会话开启；不做网络。
  - ``push_text(text)``：缓冲到内部 ``_text_buf``。
  - ``finish()``：把缓冲的整段文本一次性 POST 出去，**异步**逐块 push 到
    ``_audio_q``。
  - ``iter_audio()``：消费 ``_audio_q``，遇到 ``None`` 视为流尾。
  - ``close()``：取消进行中的 HTTP 任务并关 client。
  - ``is_alive()``：未 close、未 fail 即返回 True；HTTP 路径没有 WS 长连接，
    所以"活着"等价于"未关闭"。

为什么不在 ``push_text`` 阶段就开 POST？—— ``unidirectional`` 接口要求
请求体里就有完整 ``text``，没有像 V3 双向流那样的"先建 session 再增量送"的
动作。多次 ``push_text`` 必须先在内存里拼齐再发。新版 voice_ws 的实际
触发路径（``client_replay_tts``）总是单次 ``push_text(full_text) + finish()``，
延迟基本无差。

调试探针：``tts.start:before_connect`` 这条结构化日志保留（B3 / e2e 复用），
``data.url`` 写实际请求 URL，使其断言依然能命中 ``openspeech.bytedance.com``。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from app.core.credentials import VoiceCreds
from app.core.voice_router import DEFAULT_SPEAKER as ROUTER_DEFAULT_SPEAKER


logger = logging.getLogger(__name__)


VOLC_TTS_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
DEFAULT_TTS_RESOURCE_ID = "seed-tts-2.0"
DEFAULT_TTS_VOICE = ROUTER_DEFAULT_SPEAKER

_HTTP_CONNECT_TIMEOUT = 10.0
_HTTP_READ_TIMEOUT = 30.0


def _qidbg(location: str, message: str, data: dict | None = None) -> None:
    """与 voice_ws / passwords / rsa_keys 同口径的结构化日志通道。
    e2e B3 探针就靠 ``tts.start:before_connect`` 来判定 url 是否落在火山引擎域名。"""
    try:
        path = Path(__file__).resolve().parents[3] / ".cursor" / "debug-714cc8.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "sessionId": "P5-VOLC-TTS",
                        "runId": "be_qidbg",
                        "hypothesisId": "P5-VOICE-VOLC",
                        "location": location,
                        "message": message,
                        "data": data or {},
                        "timestamp": int(time.time() * 1000),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass


def _resolve_api_key(creds: VoiceCreds) -> str:
    """从 ``VoiceCreds`` 取唯一业务字段 ``volc_voice_key``。
    缺失则抛 ``RuntimeError`` —— 业务侧严禁回退到环境变量。"""
    key = creds.voice_key_effective()
    if not key:
        raise RuntimeError(
            "缺少火山引擎语音 API Key：请在前端"
            "[设置]→[火山引擎语音 API Key] 中填写后再开始面试。"
        )
    return key


def _resolve_voice(speaker: str | None) -> str:
    if speaker and speaker.strip():
        return speaker.strip()
    return DEFAULT_TTS_VOICE


class TtsSession:
    """火山引擎单向流式 TTS 会话（HTTP POST + 行式音频流）。

    使用方式（与旧版双向流 TtsSession 完全一致）：

        s = TtsSession(creds, speaker="...")
        await s.start()
        await s.push_text("你好，")
        await s.push_text("我是李老师。")
        await s.finish()
        async for chunk in s.iter_audio():
            ...  # 转发给前端
        await s.close()

    内部生命周期：
      1. ``start()``  → 准备 ``httpx.AsyncClient``、记录探针，不做网络。
      2. ``push_text(text)`` → 追加到 ``_text_buf``。
      3. ``finish()`` → 把 ``_text_buf`` join 后 POST 出去；后台
         ``_runner_task`` 负责按行解析 base64 chunk 并塞 ``_audio_q``。
      4. ``iter_audio()`` 消费 ``_audio_q``；遇到 ``None`` 退出。
      5. ``close()`` 关 client + 取消后台任务。
    """

    def __init__(
        self,
        creds: VoiceCreds,
        *,
        speaker: str,
        audio_format: str = "mp3",
        sample_rate: int = 24000,
        speed_ratio: float = 1.0,
    ) -> None:
        self._api_key = _resolve_api_key(creds)
        self._voice = _resolve_voice(speaker)
        self._audio_format = audio_format
        self._sample_rate = sample_rate
        self._speed_ratio = speed_ratio

        self._request_id = uuid.uuid4().hex
        self._text_buf: list[str] = []
        self._audio_q: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._runner_task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        self._error: BaseException | None = None
        self._started = False
        self._closed = False
        self._finished = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=_HTTP_CONNECT_TIMEOUT, read=_HTTP_READ_TIMEOUT,
                write=_HTTP_READ_TIMEOUT, pool=_HTTP_CONNECT_TIMEOUT,
            )
        )
        _qidbg(
            "tts.start:before_connect",
            "TtsSession ready (Volcengine V3 unidirectional, lazy POST on finish)",
            {
                "url": VOLC_TTS_URL,
                "resource_id": DEFAULT_TTS_RESOURCE_ID,
                "voice": self._voice,
                "audio_format": self._audio_format,
                "sample_rate": self._sample_rate,
                "request_id": self._request_id,
            },
        )

    def is_alive(self) -> bool:
        if not self._started or self._closed:
            return False
        if self._error is not None:
            return False
        return True

    async def push_text(self, text: str) -> None:
        if not text:
            return
        if not self._started:
            raise RuntimeError("TtsSession 尚未 start()")
        if self._closed or self._finished:
            return
        self._text_buf.append(text)

    async def finish(self) -> None:
        if not self._started or self._closed or self._finished:
            return
        self._finished = True
        full_text = "".join(self._text_buf).strip()
        if not full_text:
            await self._audio_q.put(None)
            return
        self._runner_task = asyncio.create_task(
            self._run_post(full_text), name="tts-unidirectional-runner"
        )

    async def iter_audio(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self._audio_q.get()
            if chunk is None:
                if self._error:
                    raise self._error
                return
            yield chunk

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._audio_q.put(None)
        t = self._runner_task
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def _run_post(self, text: str) -> None:
        """把整段 text POST 给火山，按行解析 base64 chunk → ``_audio_q``。"""
        if self._client is None:
            self._error = RuntimeError("TtsSession httpx client 未初始化")
            await self._audio_q.put(None)
            return

        headers = {
            "X-Api-Key": self._api_key,
            "X-Api-Resource-Id": DEFAULT_TTS_RESOURCE_ID,
            "X-Api-Request-Id": self._request_id,
        }
        payload = {
            "user": {"uid": "qi-interview"},
            "req_params": {
                "text": text,
                "speaker": self._voice,
                "audio_params": {
                    "format": self._audio_format,
                    "sample_rate": self._sample_rate,
                },
            },
        }
        # speed_ratio 通过 audio_params.speech_rate 表达：±50 范围。
        if abs(self._speed_ratio - 1.0) > 1e-3:
            payload["req_params"]["audio_params"]["speech_rate"] = int(
                round((self._speed_ratio - 1.0) * 100)
            )

        _t0 = time.perf_counter()
        _qidbg(
            "tts.post:begin",
            "POST /api/v3/tts/unidirectional begin",
            {
                "url": VOLC_TTS_URL,
                "voice": self._voice,
                "text_len": len(text),
                "request_id": self._request_id,
            },
        )

        try:
            async with self._client.stream(
                "POST", VOLC_TTS_URL, headers=headers, json=payload
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    snippet = body[:400].decode("utf-8", errors="replace")
                    self._error = RuntimeError(
                        f"火山 TTS HTTP {resp.status_code}: {snippet}"
                    )
                    _qidbg(
                        "tts.post:http_error",
                        "non-200 from /tts/unidirectional",
                        {"status": resp.status_code, "body_head": snippet[:200]},
                    )
                    await self._audio_q.put(None)
                    return

                first_chunk_logged = False
                chunks_n = 0
                bytes_total = 0
                async for raw_line in resp.aiter_lines():
                    if not raw_line:
                        continue
                    line = raw_line.strip()
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    code = obj.get("code")
                    if code not in (None, 0, 20000000):
                        msg = obj.get("message") or "unknown"
                        self._error = RuntimeError(
                            f"火山 TTS 接口错误 code={code}: {msg}"
                        )
                        _qidbg(
                            "tts.post:api_error",
                            "non-zero code from /tts/unidirectional",
                            {"code": code, "message": str(msg)[:200]},
                        )
                        break
                    data_b64 = obj.get("data")
                    if not data_b64:
                        continue
                    try:
                        chunk = base64.b64decode(data_b64)
                    except Exception:
                        continue
                    if not chunk:
                        continue
                    chunks_n += 1
                    bytes_total += len(chunk)
                    if not first_chunk_logged:
                        first_chunk_logged = True
                        _qidbg(
                            "tts.post:first_chunk",
                            "first audio chunk decoded",
                            {
                                "elapsed_ms": int((time.perf_counter() - _t0) * 1000),
                                "bytes": len(chunk),
                            },
                        )
                    await self._audio_q.put(chunk)

                _qidbg(
                    "tts.post:done",
                    "stream drained",
                    {
                        "elapsed_ms": int((time.perf_counter() - _t0) * 1000),
                        "chunks": chunks_n,
                        "bytes_total": bytes_total,
                    },
                )
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, OSError) as e:
            self._error = self._error or RuntimeError(
                f"火山 TTS 网络异常: {type(e).__name__}: {e}"
            )
            _qidbg(
                "tts.post:network_error",
                "httpx raised on /tts/unidirectional",
                {"err": f"{type(e).__name__}: {e}"[:200]},
            )
        except Exception as e:
            self._error = self._error or RuntimeError(f"火山 TTS 未预期异常: {e}")
            _qidbg(
                "tts.post:unexpected_error",
                "unexpected exception in TTS runner",
                {"err": f"{type(e).__name__}: {e}"[:200]},
            )
        finally:
            await self._audio_q.put(None)


async def synthesize_stream(
    creds: VoiceCreds,
    text: str,
    *,
    speaker: str,
    audio_format: str = "mp3",
    sample_rate: int = 24000,
    speed_ratio: float = 1.0,
) -> AsyncIterator[bytes]:
    """旧的整段合成接口（一次性传 text）。基于 ``TtsSession`` 实现。"""
    session = TtsSession(
        creds,
        speaker=speaker,
        audio_format=audio_format,
        sample_rate=sample_rate,
        speed_ratio=speed_ratio,
    )
    try:
        await session.start()
        await session.push_text(text)
        await session.finish()
        async for chunk in session.iter_audio():
            yield chunk
    finally:
        await session.close()


async def synthesize_to_bytes(
    creds: VoiceCreds, text: str, *, speaker: str, audio_format: str = "mp3"
) -> bytes:
    out = bytearray()
    async for chunk in synthesize_stream(
        creds, text, speaker=speaker, audio_format=audio_format
    ):
        out += chunk
    return bytes(out)
