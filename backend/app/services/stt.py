"""火山引擎语音 V3 流式 ASR 客户端封装（v0.4 重构）。

API: ``wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async``

鉴权头：
  - ``X-Api-Key``        = 前端 ``volc_voice_key``（透传，不回退环境变量）
  - ``X-Api-Resource-Id``= ``volc.bigasr.sauc.duration``
  - ``X-Api-Request-Id`` = 随机 UUID
  - ``X-Api-Connect-Id`` = 随机 UUID
  - ``X-Api-Sequence``   = ``"-1"``

帧格式（与 ``tests/diag/stt_test_vol.py`` 实测可用一致；不再走 voice_protocol
里的旧 V3 双向流格式）：
  - 4 字节 header：
      - INIT 帧:  ``\\x11 \\x10 \\x10 \\x00`` —— full_client_request, JSON, gzip=0
      - AUDIO 帧: ``\\x11 \\x20 \\x00 \\x00`` —— audio_only_request, raw bytes
  - 4 字节 big-endian payload size
  - payload bytes
  服务端响应：前 8 字节为 header + sequence，第 9–12 字节是 payload size，
  第 13 字节起是 JSON payload；payload 含 ``result.text`` / ``result.utterances``。

与旧版 ``SttSession`` 的 API 完全对齐，``voice_ws`` 的 ``feed`` / ``finish`` /
``iter_results`` / ``close`` / ``is_alive`` 调用点不需要改动。

为什么不复用 ``voice_protocol.py``？—— 老 V3 双向流头部里有 event ID、
session UUID 等额外字段；新接口的 ``bigmodel_async`` 头是更精简的 4 字节
flag-only 格式。强行复用反而绕弯。

调试探针：``stt.start:before_connect`` 这条结构化日志保留（B3 / e2e 复用），
``data.url`` 写实际 WSS URL，断言依然命中 ``openspeech.bytedance.com``。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import struct
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets

from app.core.credentials import VoiceCreds


logger = logging.getLogger(__name__)


VOLC_STT_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
DEFAULT_STT_RESOURCE_ID = "volc.bigasr.sauc.duration"
DEFAULT_STT_MODEL = "bigmodel"

# 4 字节固定头：第一字节 = (protocol_version<<4) | header_size = 0x11
_INIT_HEADER = b"\x11\x10\x10\x00"   # full_client_request, JSON, gzip=0
_AUDIO_HEADER = b"\x11\x20\x00\x00"  # audio_only_request, raw bytes

_FIRST_PACKET_TIMEOUT = 30.0
_TASK_RECV_TIMEOUT = 30.0


def _ws_connect_kwargs() -> dict:
    """旧版本 ``websockets`` 不支持 ``proxy=`` —— 仅在签名里有时才传。
    Windows 上传 ``proxy=None`` 可绕开 socket.getfqdn 5s blocking reverse-DNS。"""
    try:
        params = inspect.signature(websockets.connect).parameters
    except (TypeError, ValueError):
        return {}
    return {"proxy": None} if "proxy" in params else {}


_WS_EXTRA_KWARGS = _ws_connect_kwargs()


def _qidbg(location: str, message: str, data: dict | None = None) -> None:
    """与 voice_ws / passwords / rsa_keys / tts 同口径的结构化日志。
    e2e B3 探针靠 ``stt.start:before_connect`` 来判定 url 是否落在火山引擎域名。"""
    try:
        path = Path(__file__).resolve().parents[3] / ".cursor" / "debug-714cc8.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "sessionId": "P5-VOLC-STT",
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
    key = creds.voice_key_effective()
    if not key:
        raise RuntimeError(
            "缺少火山引擎语音 API Key：请在前端"
            "[设置]→[火山引擎语音 API Key] 中填写后再开始面试。"
        )
    return key


def _frame(header: bytes, payload: bytes) -> bytes:
    """组帧：4 字节 header + 4 字节 big-endian size + payload。"""
    return header + struct.pack(">I", len(payload)) + payload


@dataclass
class STTResult:
    is_final: bool
    text: str
    raw: dict


class SttSession:
    """火山引擎 ASR 流式会话（``bigmodel_async``）。

    使用方式（与 voice_ws 现有调用一致）：

        s = SttSession(creds, sample_rate=16000)
        await s.start()
        await s.feed(pcm_chunk)        # 多次
        await s.finish()
        async for r in s.iter_results():
            if r.is_final: ...
        await s.close()
    """

    def __init__(
        self,
        creds: VoiceCreds,
        *,
        sample_rate: int = 16000,
        audio_format: str = "pcm",
        hot_words: list[str] | None = None,
        max_sentence_silence: int = 600,
    ) -> None:
        self._api_key = _resolve_api_key(creds)
        self._sample_rate = sample_rate
        self._audio_format = audio_format
        self._hot_words = hot_words or []
        self._max_sentence_silence = max_sentence_silence

        self._connect_id = uuid.uuid4().hex
        self._request_id = uuid.uuid4().hex
        self._ws: Any = None
        self._results_q: asyncio.Queue[STTResult | None] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._error: BaseException | None = None
        self._started = False
        self._closed = False
        self._send_lock = asyncio.Lock()
        self._finished = False
        self._last_emitted_text = ""

    async def start(self) -> None:
        if self._started:
            return
        headers = {
            "X-Api-Key": self._api_key,
            "X-Api-Resource-Id": DEFAULT_STT_RESOURCE_ID,
            "X-Api-Request-Id": self._request_id,
            "X-Api-Connect-Id": self._connect_id,
            "X-Api-Sequence": "-1",
        }

        _t0 = time.perf_counter()
        # ── B3 探针强依赖此条日志：data.url 必须含 openspeech.bytedance.com ──
        _qidbg(
            "stt.start:before_connect",
            "calling websockets.connect (Volcengine V3 bigmodel_async)",
            {
                "url": VOLC_STT_URL,
                "resource_id": DEFAULT_STT_RESOURCE_ID,
                "sample_rate": self._sample_rate,
                "audio_format": self._audio_format,
                "connect_id": self._connect_id,
            },
        )

        try:
            self._ws = await websockets.connect(
                VOLC_STT_URL,
                additional_headers=headers,
                max_size=1024 * 1024 * 16,
                open_timeout=15,
                ping_interval=None,
                **_WS_EXTRA_KWARGS,
            )
        except (websockets.WebSocketException, asyncio.TimeoutError, OSError) as e:
            raise RuntimeError(
                f"火山 STT 连接失败: {type(e).__name__}: {e}"
            ) from e

        _t_conn = time.perf_counter()
        _qidbg(
            "stt.start:after_connect",
            "websockets.connect returned",
            {"connect_ms": int((_t_conn - _t0) * 1000), "url": VOLC_STT_URL},
        )

        # ── INIT 帧（full_client_request, JSON）──
        init_request = {
            "user": {"uid": "qi-interview"},
            "audio": {
                "format": self._audio_format,
                "rate": self._sample_rate,
                "bits": 16,
                "channel": 1,
            },
            "request": {
                "model_name": DEFAULT_STT_MODEL,
                "enable_itn": True,
                "enable_punc": True,
                "enable_nonstream": True,
            },
        }
        if self._hot_words:
            init_request["request"]["hot_words"] = list(self._hot_words)

        try:
            payload = json.dumps(init_request, ensure_ascii=False).encode("utf-8")
            await self._ws.send(_frame(_INIT_HEADER, payload))
        except (websockets.WebSocketException, OSError) as e:
            await self._safe_close_ws()
            raise RuntimeError(
                f"火山 STT INIT 帧发送失败: {type(e).__name__}: {e}"
            ) from e

        _qidbg(
            "stt.start:init_sent",
            "INIT frame sent, ready for audio",
            {
                "total_ms": int((time.perf_counter() - _t0) * 1000),
                "request_id": self._request_id,
            },
        )

        self._started = True
        self._reader_task = asyncio.create_task(self._reader_loop(), name="stt-reader")

    def is_alive(self) -> bool:
        if not self._started or self._closed:
            return False
        if self._error is not None:
            return False
        rt = self._reader_task
        if rt is None or rt.done():
            return False
        ws = self._ws
        if ws is None:
            return False
        try:
            if getattr(ws, "closed", False):
                return False
        except Exception:
            return False
        return True

    async def feed(self, pcm: bytes) -> None:
        if not pcm or not self._started or self._closed or self._ws is None:
            return
        if self._finished:
            return
        async with self._send_lock:
            try:
                await self._ws.send(_frame(_AUDIO_HEADER, pcm))
            except (websockets.WebSocketException, OSError) as e:
                logger.warning("火山 STT feed 失败: %s", e)
                self._error = self._error or RuntimeError(str(e))
                await self._results_q.put(None)

    async def finish(self) -> None:
        if not self._started or self._finished or self._closed or self._ws is None:
            return
        self._finished = True
        async with self._send_lock:
            try:
                # 发一包零字节 audio 帧表示流结束（与 stt_test_vol.py 实测一致：
                # 服务端在 finish 后会自动出最后一句 final 并关流）。
                await self._ws.send(_frame(_AUDIO_HEADER, b""))
            except (websockets.WebSocketException, OSError) as e:
                logger.warning("火山 STT finish 失败: %s", e)
                self._error = self._error or RuntimeError(str(e))
                await self._results_q.put(None)

    async def iter_results(self) -> AsyncIterator[STTResult]:
        while True:
            r = await self._results_q.get()
            if r is None:
                if self._error:
                    raise self._error
                return
            yield r

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._results_q.put(None)
        t = self._reader_task
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await self._safe_close_ws()

    async def _safe_close_ws(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is None:
            return
        try:
            await ws.close()
        except Exception:
            pass

    @staticmethod
    def _emit_from_payload(payload_json: dict) -> tuple[bool, str]:
        """从 server 帧 payload 抽取 (is_final, text)。

        典型 payload 形态：
          ``{"result": {"text": "...", "utterances":[{"text":..., "definite": True|False}, ...]}}``

        策略与旧版一致：
          - 优先看最近一条 utterance 的 ``definite`` 标志；
          - 没 utterance 但有 ``result.text`` 当作 partial 输出。
        """
        result = payload_json.get("result") or {}
        utterances = result.get("utterances") or []
        if utterances:
            last = utterances[-1]
            text = last.get("text") or ""
            is_final = bool(last.get("definite"))
            return is_final, text
        text = result.get("text") or ""
        return False, text

    @staticmethod
    def _parse_response(raw: bytes) -> dict | None:
        """``bigmodel_async`` 服务端响应解析：前 12 字节是头部，第 8–12 字节
        big-endian size，剩下是 JSON。失败返回 None（外层忽略）。"""
        if not raw or len(raw) < 12:
            return None
        try:
            payload_size = struct.unpack(">I", raw[8:12])[0]
            payload = raw[12 : 12 + payload_size]
            return json.loads(payload.decode("utf-8", errors="replace"))
        except (struct.error, json.JSONDecodeError, UnicodeDecodeError):
            return None

    async def _reader_loop(self) -> None:
        ws = self._ws
        assert ws is not None
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=_TASK_RECV_TIMEOUT)
                except asyncio.TimeoutError:
                    if self._closed:
                        break
                    continue
                if isinstance(raw, str):
                    # bigmodel_async 不应发文本帧，忽略
                    continue
                payload = self._parse_response(raw)
                if payload is None:
                    continue
                # 顶层错误（部分版本的服务端会用 ``code != 0`` 表示失败）
                code = payload.get("code")
                if code is not None and int(code) not in (0, 20000000, 1013):
                    msg = payload.get("message") or "unknown"
                    self._error = RuntimeError(
                        f"火山 STT 接口错误 code={code}: {msg}"
                    )
                    await self._results_q.put(None)
                    return
                is_final, text = self._emit_from_payload(payload)
                if text or is_final:
                    self._last_emitted_text = text or self._last_emitted_text
                    await self._results_q.put(
                        STTResult(is_final=is_final, text=text, raw=payload)
                    )
                # finish 路径下，服务端可能用 ``end_of_stream`` / ``last_package``
                # 字段表示流结束。任意一处出现即认为流尾，关 queue 让上层退出。
                if (
                    payload.get("end_of_stream")
                    or payload.get("last_package")
                    or payload.get("is_last_package")
                ):
                    if self._finished:
                        await self._results_q.put(None)
                        return
        except asyncio.CancelledError:
            raise
        except (websockets.ConnectionClosed, OSError) as e:
            self._error = self._error or RuntimeError(
                f"火山 STT 连接异常: {type(e).__name__}: {e}"
            )
            await self._results_q.put(None)
        except Exception as e:
            self._error = self._error or RuntimeError(f"火山 STT reader error: {e}")
            await self._results_q.put(None)


async def recognize_stream(
    creds: VoiceCreds,
    audio_chunks: AsyncIterator[bytes],
    *,
    sample_rate: int = 16000,
    audio_format: str = "pcm",
    hot_words: list[str] | None = None,
) -> AsyncIterator[STTResult]:
    """旧接口：传入音频迭代器、返回识别结果迭代器。基于 ``SttSession`` 实现。"""
    session = SttSession(
        creds,
        sample_rate=sample_rate,
        audio_format=audio_format,
        hot_words=hot_words,
    )

    async def _feeder() -> None:
        try:
            async for chunk in audio_chunks:
                if not chunk:
                    continue
                await session.feed(chunk)
        except Exception as e:
            logger.warning("recognize_stream feeder error: %s", e)
        finally:
            await session.finish()

    try:
        await session.start()
        feeder_task = asyncio.create_task(_feeder(), name="volc-stt-feeder")
        try:
            async for r in session.iter_results():
                yield r
        finally:
            if not feeder_task.done():
                feeder_task.cancel()
                try:
                    await feeder_task
                except (asyncio.CancelledError, Exception):
                    pass
    finally:
        await session.close()


async def recognize_bytes(
    creds: VoiceCreds,
    audio: bytes,
    *,
    sample_rate: int = 16000,
    audio_format: str = "pcm",
) -> str:
    """便捷 API：一次性识别一整段 PCM 音频。"""
    chunk_size = 3200  # 100ms @ 16k 16bit mono

    async def _gen() -> AsyncIterator[bytes]:
        for i in range(0, len(audio), chunk_size):
            yield audio[i : i + chunk_size]
            await asyncio.sleep(0.01)

    last_text = ""
    async for r in recognize_stream(
        creds, _gen(), sample_rate=sample_rate, audio_format=audio_format
    ):
        if r.text:
            last_text = r.text
    return last_text
