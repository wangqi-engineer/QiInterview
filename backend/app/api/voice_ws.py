"""面试 WebSocket：协调 STT → 面试官引擎 → TTS。

事件协议（JSON 文本帧；音频用 base64 嵌入）：

Client → Server:
  {"type": "auth",  "llm_*": "...", "volc_voice_key": "..."}      # 首帧鉴权
  {"type": "start"}                                                # 触发开场白
  {"type": "audio_chunk", "pcm_base64": "..."}                     # 16k mono PCM Int16
  {"type": "answer_text", "text": "..."}                           # 用户文本作答（textarea + 发送本轮回答）
  {"type": "end_turn"}                                             # 候选人确认本轮结束（兜底 force final）
  {"type": "user_interrupt"}                                       # 候选人开始说话，打断 AI
  {"type": "client_replay_tts", "text": "..."}                     # 朗读按钮：把指定 AI 文本回送 TTS
  {"type": "end_interview"}                                        # 主动结束

Server → Client:
  {"type": "ai_thinking"}
  {"type": "ai_text", "text": "...", "strategy": "...", "expected_topic": "..."}
  {"type": "ai_audio", "mime": "audio/mp3", "chunk_b64": "..."}
  {"type": "ai_audio_end"}
  {"type": "stt_partial", "text": "..."}
  {"type": "stt_final",   "text": "...", "turn_idx": 12}
  {"type": "score_update", "turn_idx": 12, "delta": -3, "total": 71, "evaluator": {...}}
  {"type": "ai_interrupt", "reason": "off_topic"|"too_long"}
  {"type": "interview_end", "reason": "user"|"score_threshold"|"complete"}
  {"type": "error", "message": "..."}

v0.4 重构要点：
  - 凭据：auth 首帧的 ``volc_voice_key`` 是语音通道的**唯一**来源。
    业务侧严禁回退到 ``Settings``。
  - TTS：火山 ``api/v3/tts/unidirectional`` 走 HTTP POST，不再有 5–10 s 的
    WS 握手；warm pool / preopened / filler-cache 全部移除。
  - 音色：``pick_speaker(interview_type)`` 优先从 ``Settings`` 读 env
    覆盖，留空回退 ``VOICE_MAP``。
  - 自动 TTS：opening / next_question / wrap_up 的 ``auto_tts=False``，AI
    文字到达后等用户点 [朗读] 按钮（``client_replay_tts``）才合成语音。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.credentials import (
    LLMCreds,
    VoiceCreds,
    llm_creds_from_query,
    voice_creds_from_query,
)
from app.core.voice_router import pick_speaker
from app.db.session import AsyncSessionLocal
from app.models.interview import InterviewSession, Turn
from app.services import llm_mock
from app.services.interviewer import InterviewerEngine, TurnRecord
from app.services.stt import SttSession
from app.services.tts import TtsSession


logger = logging.getLogger(__name__)
router = APIRouter()


_VOICE_WS_BUILD_ID = "VOICE-WS-BUILD-VOLC-V04"
import sys as _sys_for_build
print(f"[voice_ws] module loaded build={_VOICE_WS_BUILD_ID}", file=_sys_for_build.stderr, flush=True)


def _safe_speaker(stored: str | None, interview_type: str) -> str:
    """旧 DB 行存的 speaker ID 已经过期（CosyVoice / 阿里 longxiao_v2）。
    新版本一律按 ``pick_speaker(interview_type)`` 重解析；只有当 DB
    存的明确不是火山系（不带 ``_bigtts`` 后缀也不带 ``zh_`` 前缀）才
    保留 —— 这种情况几乎不存在，留个口子方便用户手工指定。"""
    s = (stored or "").strip()
    if not s or "_bigtts" in s or s.startswith("zh_") or s.startswith("longxiao"):
        return pick_speaker(interview_type)
    return s


async def _safe_close_tts(s: TtsSession) -> None:
    try:
        await s.close()
    except Exception:
        pass


# #region agent log
def _qidbg(location: str, message: str, data: dict | None = None) -> None:
    """Append one NDJSON line to .cursor/debug-714cc8.log for session 714cc8."""
    try:
        path = Path(__file__).resolve().parent.parent.parent.parent / ".cursor" / "debug-714cc8.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "sessionId": "714cc8",
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
# #endregion


class _SessionContext:
    """一个 WS 连接的运行时上下文，集中管理 LLM/TTS/STT 的并发与取消。"""

    def __init__(
        self,
        ws: WebSocket,
        sid: str,
        engine: InterviewerEngine,
        voice_creds: VoiceCreds,
        speaker: str,
    ) -> None:
        self.ws = ws
        self.sid = sid
        self.engine = engine
        self.voice_creds = voice_creds
        self.speaker = speaker

        self.tts_play_task: asyncio.Task | None = None
        self.current_tts: TtsSession | None = None
        self.stt_session: SttSession | None = None
        self.stt_consumer_task: asyncio.Task | None = None
        self.processing_lock = asyncio.Lock()
        self.ended: bool = False
        self._send_lock = asyncio.Lock()
        # 跟踪最近一条非空 partial：当客户端发 ``end_turn`` 而服务端 STT 还没
        # 出 final（fake-mic 无缝循环 / 静音不足）时，用 partial 兜底触发
        # _on_user_final，避免整条 STT-LLM-TTS 流水线死等。
        self.last_partial_text: str = ""
        # ``filler_lock`` 历史上用于序列化 eager filler 与 LLM-driven TTS。
        # v0.4 起 eager filler 已退役（manual TTS 不允许 AI 主动出声），
        # lock 仍保留为 ``play_text_stream`` 之间的串行闸，防止两条并发
        # client_replay_tts 把 ai_audio 帧交错。
        self.filler_lock: asyncio.Lock = asyncio.Lock()
        # 真凭据 + 非 mock 才走 STT/TTS 真链路。
        self.has_voice = bool(voice_creds.has_voice_creds()) and not llm_mock.is_mock_enabled()

    # ---- helpers ----

    async def send_json(self, msg: dict[str, Any]) -> None:
        try:
            async with self._send_lock:
                await self.ws.send_text(json.dumps(msg, ensure_ascii=False))
        except Exception:
            pass

    async def cancel_tts(self) -> None:
        """打断当前正在合成 / 播放的 TTS（不影响 STT）。"""
        t = self.tts_play_task
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self.tts_play_task = None
        s = self.current_tts
        self.current_tts = None
        if s is not None:
            try:
                await s.close()
            except Exception:
                pass

    # ---- STT 长连接 ----

    async def ensure_stt(self) -> None:
        """确保常驻 STT 长连接已就绪；失败时静默降级（has_voice=False 即跳过）。

        i11 修复：上一轮 ``end_turn`` → ``stt_session.finish()`` 之后，
        服务端会发流尾包关掉本次 task；此时 SttSession 虽然对象还在
        （``_finished=True``），但已经吃不到任何新音频。本方法把"会话还活
        着"作为复用条件——只要 finish/close 过就视为死掉，重置后开新一条。
        """
        if not self.has_voice:
            return
        sess_existing = self.stt_session
        if sess_existing is not None and (
            getattr(sess_existing, "_finished", False)
            or getattr(sess_existing, "_closed", False)
        ):
            old_consumer = self.stt_consumer_task
            self.stt_session = None
            self.stt_consumer_task = None
            try:
                await sess_existing.close()
            except Exception:
                pass
            if old_consumer is not None and not old_consumer.done():
                old_consumer.cancel()
                try:
                    await old_consumer
                except (asyncio.CancelledError, Exception):
                    pass
        if self.stt_session is not None:
            return
        try:
            sess = SttSession(
                self.voice_creds,
                sample_rate=16000,
                audio_format="pcm",
                max_sentence_silence=600,
            )
            await sess.start()
            self.stt_session = sess
            self.stt_consumer_task = asyncio.create_task(
                self._stt_consumer_loop(), name="stt-consumer"
            )
            _qidbg(
                "voice_ws.ensure_stt:opened",
                "fresh SttSession started",
                {"sid": self.sid},
            )
        except Exception as e:
            logger.warning("STT 长连接建立失败，降级到无 STT 模式: %s", e)
            self.has_voice = False

    async def feed_audio(self, pcm: bytes) -> None:
        if not self.has_voice:
            return
        await self.ensure_stt()
        if self.stt_session is None:
            return
        try:
            await self.stt_session.feed(pcm)
        except Exception as e:
            logger.warning("STT feed 失败: %s", e)

    async def _stt_consumer_loop(self) -> None:
        sess = self.stt_session
        if sess is None:
            return
        try:
            async for r in sess.iter_results():
                if not r.text and not r.is_final:
                    continue
                if not r.is_final:
                    if r.text:
                        self.last_partial_text = r.text
                    await self.send_json({"type": "stt_partial", "text": r.text})
                    continue
                _qidbg(
                    "voice_ws._stt_consumer_loop:final",
                    "stt final received; forwarding to client (no auto _on_user_final)",
                    {
                        "sid": self.sid,
                        "text_len": len((r.text or "").strip()),
                    },
                )
                text = (r.text or "").strip()
                if not text:
                    continue
                self.last_partial_text = ""
                cand_idx = len(self.engine.history) + 1
                await self.send_json(
                    {"type": "stt_final", "text": text, "turn_idx": cand_idx}
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("STT consumer 异常: %s", e)
            _qidbg(
                "voice_ws._stt_consumer_loop:exception",
                "STT consumer raised; emitting error frame and degrading has_voice=False",
                {
                    "sid": self.sid,
                    "err": f"{type(e).__name__}: {e}"[:240],
                },
            )
            await self.send_json(
                {"type": "error", "message": f"STT 异常: {e}（已降级，请改用文本作答）"}
            )
            self.has_voice = False
        finally:
            if self.stt_session is sess:
                self.stt_session = None

    async def _on_user_final(self, text: str, *, emit_stt_final: bool = True) -> None:
        """触发评分 + 下一题。

        ``emit_stt_final``：
          - 默认 True：``end_turn`` 兜底 fallback 路径用，把 ``stt_final`` 推
            到客户端 UI；
          - 当通过 ``answer_text`` 手动提交时由调用方传 ``False`` —— 文本
            是用户在 [input-text-answer] 编辑后送的，再回推一条 stt_final
            会让前端再 setTextAnswer(text)，把刚 clear 的 textarea 又填回
            原文 → UX 混乱。
        """
        _qidbg(
            "voice_ws._on_user_final:enter",
            "awaiting processing_lock",
            {
                "sid": self.sid,
                "text_len": len(text),
                "locked": self.processing_lock.locked(),
                "emit_stt_final": emit_stt_final,
            },
        )
        async with self.processing_lock:
            _qidbg(
                "voice_ws._on_user_final:in_lock",
                "lock acquired, will process",
                {"sid": self.sid, "ended": self.ended, "history_len": len(self.engine.history)},
            )
            if self.ended:
                return
            if emit_stt_final:
                cand_idx = len(self.engine.history) + 1
                await self.send_json(
                    {"type": "stt_final", "text": text, "turn_idx": cand_idx}
                )
            await _process_answer(self, text)

    # ---- TTS 流式播放 ----

    async def play_text_stream(
        self,
        chunks: "asyncio.Queue[str | None]",
    ) -> None:
        """从 ``chunks`` 队列消费文本片段，喂给 TtsSession，同时把音频转发给前端。

        - 若 ``has_voice=False``（无凭据 / mock / TTS 失败），仅在收到 None
          时发 ``ai_audio_end``。
        - 失败后自动降级到无音频模式，避免影响后续轮次。

        ``filler_lock`` 用作 ``play_text_stream`` 之间的串行闸，防止两条
        并发 client_replay_tts 把 ai_audio 帧交错。
        """
        async with self.filler_lock:
            await self._play_text_stream_inner(chunks)

    async def _play_text_stream_inner(
        self,
        chunks: "asyncio.Queue[str | None]",
    ) -> None:
        if not self.has_voice:
            _qidbg(
                "voice_ws.play_text_stream:no_voice",
                "skipping TTS because has_voice=False",
                {"sid": self.sid},
            )
            try:
                while True:
                    item = await chunks.get()
                    if item is None:
                        break
            except asyncio.CancelledError:
                raise
            await self.send_json(
                {"type": "ai_audio_end", "skipped": True, "reason": "no_voice_creds"}
            )
            return

        # v0.4：火山 unidirectional HTTP TTS 没有 WS 握手开销，每段直接
        # 建一个 TtsSession。``httpx.AsyncClient`` 内部的连接复用会让连续
        # POST 命中同一条 keep-alive TCP，与旧 warm pool 的收益基本相当。
        tts: TtsSession | None = None
        _tts_t0 = time.perf_counter()
        try:
            tts = TtsSession(
                self.voice_creds, speaker=self.speaker, audio_format="mp3"
            )
            _qidbg(
                "voice_ws.play_text_stream:tts_start_begin",
                "calling TtsSession.start (Volcengine V3 unidirectional)",
                {"sid": self.sid},
            )
            await tts.start()
            _qidbg(
                "voice_ws.play_text_stream:tts_start_end",
                "TtsSession.start completed",
                {
                    "sid": self.sid,
                    "elapsed_ms": int((time.perf_counter() - _tts_t0) * 1000),
                },
            )
        except Exception as e:
            logger.warning("TTS start 失败，降级到无音频: %s", e)
            _qidbg(
                "voice_ws.play_text_stream:tts_start_fail",
                "TtsSession.start() failed, downgrading to text-only",
                {"sid": self.sid, "err": str(e)[:200]},
            )
            self.has_voice = False
            try:
                while True:
                    item = await chunks.get()
                    if item is None:
                        break
            except asyncio.CancelledError:
                raise
            await self.send_json(
                {"type": "ai_audio_end", "skipped": True, "reason": "tts_unavailable"}
            )
            return

        self.current_tts = tts

        async def _producer() -> None:
            _push_chunks_n = 0
            _push_chars_total = 0
            try:
                while True:
                    item = await chunks.get()
                    if item is None:
                        _qidbg(
                            "voice_ws.tts:input_done",
                            "chunks queue drained, calling tts.finish",
                            {
                                "sid": self.sid,
                                "tts_input_chunks": _push_chunks_n,
                                "tts_input_chars": _push_chars_total,
                            },
                        )
                        await tts.finish()
                        return
                    if item:
                        _push_chunks_n += 1
                        _push_chars_total += len(item)
                        # ``text_len`` 字段：被 push_text 实际接收的字符数。
                        # i14 用 ``re.search('text_len')`` 累加，与"用户合同
                        # 的朗读文本总字数"对照，断言空格/标点不会让 TTS 漏字。
                        _qidbg(
                            "voice_ws.tts:input",
                            "pushing chunk into TtsSession",
                            {
                                "sid": self.sid,
                                "n": _push_chunks_n,
                                "text_len": len(item),
                                "text_head": item[:32],
                                "running_total_chars": _push_chars_total,
                            },
                        )
                        await tts.push_text(item)
            except asyncio.CancelledError:
                try:
                    await tts.finish()
                except Exception:
                    pass
                raise

        async def _consumer() -> None:
            _ai_audio_n = 0
            try:
                async for chunk in tts.iter_audio():
                    if not chunk:
                        continue
                    _ai_audio_n += 1
                    if _ai_audio_n <= 3:
                        _qidbg(
                            "voice_ws._consumer:ai_audio",
                            "sending ai_audio frame",
                            {"sid": self.sid, "n": _ai_audio_n, "bytes": len(chunk)},
                        )
                    await self.send_json(
                        {
                            "type": "ai_audio",
                            "mime": "audio/mp3",
                            "chunk_b64": base64.b64encode(chunk).decode(),
                        }
                    )
            except asyncio.CancelledError:
                raise

        prod = asyncio.create_task(_producer(), name="tts-producer")
        cons = asyncio.create_task(_consumer(), name="tts-consumer")
        try:
            await asyncio.gather(prod, cons)
            await self.send_json({"type": "ai_audio_end"})
        except asyncio.CancelledError:
            for t in (prod, cons):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
            await self.send_json({"type": "ai_audio_end", "interrupted": True})
            raise
        except Exception as e:
            logger.warning("TTS 播放失败，本段降级到无音频: %s", e)
            for t in (prod, cons):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
            try:
                while True:
                    item = chunks.get_nowait()
                    if item is None:
                        break
            except asyncio.QueueEmpty:
                pass
            self.has_voice = False
            await self.send_json(
                {
                    "type": "ai_audio_end",
                    "skipped": True,
                    "reason": "tts_runtime_error",
                    "message": str(e),
                }
            )
        finally:
            try:
                await tts.close()
            except Exception:
                pass
            if self.current_tts is tts:
                self.current_tts = None


# ---------- DB helpers ----------


async def _load_session(sid: str) -> InterviewSession | None:
    async with AsyncSessionLocal() as db:
        stmt = (
            select(InterviewSession)
            .where(InterviewSession.id == sid)
            .options(selectinload(InterviewSession.turns))
        )
        return (await db.execute(stmt)).scalar_one_or_none()


async def _persist_turn(sid: str, t: TurnRecord) -> None:
    async with AsyncSessionLocal() as db:
        db.add(
            Turn(
                session_id=sid,
                idx=t.idx,
                role=t.role,
                text=t.text,
                strategy=t.strategy,
                expected_topic=t.expected_topic,
                score_delta=t.score_delta,
                score_after=t.score_after,
                evaluator_json=t.evaluator_json,
                ended_at=datetime.utcnow(),
            )
        )
        await db.commit()


async def _persist_final(sid: str, score: int, end_reason: str) -> None:
    async with AsyncSessionLocal() as db:
        row = await db.get(InterviewSession, sid)
        if row:
            row.final_score = score
            row.end_reason = end_reason
            row.ended_at = datetime.utcnow()
            await db.commit()


async def _maybe_finalize_with_partial(ctx: _SessionContext) -> None:
    """在客户端 end_turn 之后给服务端 STT 留 ~400 ms 的窗口去补发
    ``is_final``；窗口内成功补发则正常路径已被 ``_stt_consumer_loop`` 触发，
    本协程感知到后即返回；窗口超时仍无 final 但缓存里有 partial，则手动
    用 partial 兜底进 ``_on_user_final``，确保 STT-LLM-TTS 流水线不死等。
    """
    _qidbg(
        "voice_ws._maybe_finalize_with_partial:enter",
        "fallback wait window started",
        {
            "sid": ctx.sid,
            "snapshot_len": len(ctx.last_partial_text or ""),
            "lock_busy": ctx.processing_lock.locked(),
        },
    )
    deadline = time.perf_counter() + 0.4
    snapshot = ctx.last_partial_text
    while time.perf_counter() < deadline:
        await asyncio.sleep(0.05)
        if ctx.ended:
            _qidbg(
                "voice_ws._maybe_finalize_with_partial:exit",
                "ctx ended during wait",
                {"sid": ctx.sid},
            )
            return
        if not ctx.last_partial_text:
            _qidbg(
                "voice_ws._maybe_finalize_with_partial:exit",
                "real final detected (partial cleared by consumer)",
                {"sid": ctx.sid},
            )
            return
        if ctx.processing_lock.locked():
            _qidbg(
                "voice_ws._maybe_finalize_with_partial:exit",
                "processing already in flight",
                {"sid": ctx.sid},
            )
            return
    if not ctx.last_partial_text or ctx.processing_lock.locked():
        _qidbg(
            "voice_ws._maybe_finalize_with_partial:exit",
            "post-deadline conditions changed; not firing",
            {
                "sid": ctx.sid,
                "partial_len": len(ctx.last_partial_text or ""),
                "lock_busy": ctx.processing_lock.locked(),
            },
        )
        return
    text = (snapshot or ctx.last_partial_text or "").strip()
    if not text:
        _qidbg(
            "voice_ws._maybe_finalize_with_partial:exit",
            "no partial text to use as fallback",
            {"sid": ctx.sid},
        )
        return
    _qidbg(
        "voice_ws._maybe_finalize_with_partial:fire",
        "no STT final within 400ms, falling back to last partial",
        {"sid": ctx.sid, "text_len": len(text)},
    )
    ctx.last_partial_text = ""
    await ctx._on_user_final(text)


# ---------- 核心 pipeline：流式 LLM → 流式 TTS ----------


async def _drive_speech_stream(
    ctx: _SessionContext,
    ev_iter,  # AsyncIterator[dict] from engine.stream_*
    *,
    auto_tts: bool = True,
) -> dict[str, Any]:
    """通用驱动：把引擎的 (speech_chunk / speech_done / done) 事件流接到 TTS+前端。

    返回引擎给出的 ``done.data``（含 speech / strategy / expected_topic /
    end_reason 等）。

    ``auto_tts``（i12 / i13 / i14 / v0.2 起的"语音手动化"重设计）：
      - ``False``（默认在 opening / next_question / wrap_up）：本协程**不**
        启动 ``play_text_stream``、不发 ``ai_audio*`` 帧，仅把 ``speech_chunk``
        累积成最终 ``ai_text``。AI 的语音由用户点 [朗读] 按钮（前端发
        ``client_replay_tts`` 协议）触发。
      - ``True``：保留旧路径以备调用点切回；当前业务流程不再使用。
    """
    if not auto_tts:
        speech_acc: list[str] = []
        final_data: dict[str, Any] = {}
        try:
            async for ev in ev_iter:
                etype = ev.get("type")
                if etype == "speech_chunk":
                    piece = str(ev.get("text") or "")
                    if piece:
                        speech_acc.append(piece)
                elif etype == "speech_done":
                    pass
                elif etype == "done":
                    final_data = ev.get("data") or {}
        except asyncio.CancelledError:
            raise
        full = (final_data.get("speech") or "").strip() or "".join(speech_acc).strip()
        if full:
            await ctx.send_json(
                {
                    "type": "ai_text",
                    "text": full,
                    "strategy": str(final_data.get("strategy") or ""),
                    "expected_topic": str(final_data.get("expected_topic") or ""),
                }
            )
        return final_data

    text_chunks: asyncio.Queue[str | None] = asyncio.Queue()
    play_task = asyncio.create_task(
        ctx.play_text_stream(text_chunks), name="play-text-stream"
    )
    ctx.tts_play_task = play_task

    speech_acc: list[str] = []
    final_data: dict[str, Any] = {}

    _ds_t0 = time.perf_counter()
    _first_chunk_logged = False
    _qidbg(
        "voice_ws._drive_speech_stream:enter",
        "drive speech stream started, awaiting engine events",
        {"sid": ctx.sid},
    )
    try:
        async for ev in ev_iter:
            etype = ev.get("type")
            if etype == "speech_chunk":
                piece = str(ev.get("text") or "")
                if piece:
                    if not _first_chunk_logged:
                        _qidbg(
                            "voice_ws._drive_speech_stream:first_speech_chunk",
                            "first speech_chunk from engine queued for TTS",
                            {
                                "sid": ctx.sid,
                                "elapsed_ms": int((time.perf_counter() - _ds_t0) * 1000),
                                "chars": len(piece),
                            },
                        )
                        _first_chunk_logged = True
                    speech_acc.append(piece)
                    await text_chunks.put(piece)
            elif etype == "speech_done":
                _qidbg(
                    "voice_ws._drive_speech_stream:speech_done",
                    "engine signaled speech_done (text fully streamed)",
                    {
                        "sid": ctx.sid,
                        "elapsed_ms": int((time.perf_counter() - _ds_t0) * 1000),
                        "chunks_count": len(speech_acc),
                    },
                )
            elif etype == "done":
                final_data = ev.get("data") or {}
    except asyncio.CancelledError:
        await text_chunks.put(None)
        if not play_task.done():
            play_task.cancel()
        raise

    full = (final_data.get("speech") or "").strip() or "".join(speech_acc).strip()
    if full:
        await ctx.send_json(
            {
                "type": "ai_text",
                "text": full,
                "strategy": str(final_data.get("strategy") or ""),
                "expected_topic": str(final_data.get("expected_topic") or ""),
            }
        )

    await text_chunks.put(None)
    try:
        await play_task
    except asyncio.CancelledError:
        pass
    if ctx.tts_play_task is play_task:
        ctx.tts_play_task = None

    return final_data


async def _opening_pipeline(ctx: _SessionContext) -> None:
    _qidbg(
        "voice_ws._opening_pipeline:enter",
        "opening pipeline started",
        {"sid": ctx.sid, "has_voice": ctx.has_voice},
    )
    await ctx.send_json({"type": "ai_thinking"})
    try:
        # i12 / 语音手动化：opening 不再自动播 TTS。AI 文字到达后等用户点
        # [朗读] 按钮，前端发 ``client_replay_tts`` 才合成语音。
        data = await _drive_speech_stream(
            ctx, ctx.engine.stream_opening(), auto_tts=False
        )
    except Exception as e:
        logger.exception("opening 失败: %s", e)
        _qidbg(
            "voice_ws._opening_pipeline:exception",
            "opening pipeline raised",
            {"err": str(e)[:200]},
        )
        await ctx.send_json({"type": "error", "message": f"开场失败: {e}"})
        return
    _qidbg(
        "voice_ws._opening_pipeline:done",
        "opening drive_speech_stream returned",
        {"has_data": bool(data), "speech_len": len(str((data or {}).get("speech") or ""))},
    )
    await _persist_turn(ctx.sid, ctx.engine.history[-1])
    await ctx.ensure_stt()


async def _next_question_pipeline(
    ctx: _SessionContext, *, force_wrap_reason: str | None = None
) -> None:
    await ctx.send_json({"type": "ai_thinking"})
    # i12 / 语音手动化（v0.2）：next_question / wrap_up 不再自动播 TTS。
    try:
        if force_wrap_reason:
            data = await _drive_speech_stream(
                ctx,
                ctx.engine.stream_wrap_up(reason=force_wrap_reason),
                auto_tts=False,
            )
        else:
            data = await _drive_speech_stream(
                ctx,
                ctx.engine.stream_next_question(),
                auto_tts=False,
            )
    except Exception as e:
        logger.exception("next_question 失败: %s", e)
        await ctx.send_json({"type": "error", "message": f"出题失败: {e}"})
        return

    last_turn = ctx.engine.history[-1]
    await _persist_turn(ctx.sid, last_turn)

    if data.get("strategy") == "wrap_up" or last_turn.strategy == "wrap_up":
        end_reason = data.get("end_reason") or force_wrap_reason or "complete"
        ctx.ended = True
        await _persist_final(ctx.sid, ctx.engine.current_score, end_reason)
        await ctx.send_json({"type": "interview_end", "reason": end_reason})


async def _process_answer(ctx: _SessionContext, answer_text: str) -> None:
    """对一段候选人回答执行：**并行**评估（~9s LLM）和下一题流式（~10s LLM
    TTFT）。串行 ~19s，并行后 ``max(9, 10) ≈ 10s``。

    流程：
    1. 立即把 candidate turn 占位入 history（``append_candidate_turn``），
       这样 ``stream_next_question`` 能看到这一轮上下文；
    2. **并行启动**两条 LLM：
        - ``evaluate_existing_turn`` 走 fast 档非流式，回填 score_delta；
        - ``_next_question_pipeline`` 走 fast 档流式。
    3. 评估完成后：发 ``score_update`` / 可选 ``ai_interrupt``、持久化 candidate；
    4. 等下一题 pipeline 结束。
    """
    if not answer_text.strip() or ctx.ended:
        return

    await ctx.send_json({"type": "ai_thinking"})

    cand_turn = ctx.engine.append_candidate_turn(answer_text)

    async def _eval() -> dict[str, Any]:
        try:
            return await ctx.engine.evaluate_existing_turn(cand_turn, answer_text)
        except Exception as e:
            logger.warning("evaluate_answer 失败: %s", e)
            return {"delta": 0, "score": ctx.engine.current_score, "evaluator": {}}

    eval_task = asyncio.create_task(_eval(), name="ev-eval")
    nq_task = asyncio.create_task(
        _next_question_pipeline(ctx), name="ev-next-question"
    )

    async def _on_eval_done() -> None:
        try:
            res = await eval_task
        except Exception as e:
            logger.warning("evaluate_answer task 异常: %s", e)
            res = {"delta": 0, "score": ctx.engine.current_score, "evaluator": {}}
        try:
            await _persist_turn(ctx.sid, cand_turn)
            _qidbg(
                "voice_ws._process_answer:persisted_candidate",
                "candidate turn persisted to DB",
                {
                    "sid": ctx.sid,
                    "turn_idx": cand_turn.idx,
                    "role": cand_turn.role,
                    "text_len": len(cand_turn.text or ""),
                },
            )
            await ctx.send_json(
                {
                    "type": "score_update",
                    "turn_idx": cand_turn.idx,
                    "delta": res["delta"],
                    "total": res["score"],
                    "evaluator": res.get("evaluator") or {},
                }
            )
            evaluator = res.get("evaluator") or {}
            if evaluator.get("off_topic"):
                await ctx.send_json({"type": "ai_interrupt", "reason": "off_topic"})
            elif evaluator.get("too_long"):
                await ctx.send_json({"type": "ai_interrupt", "reason": "too_long"})
        except Exception:
            pass

    eval_done_task = asyncio.create_task(_on_eval_done(), name="ev-eval-done")

    try:
        await nq_task
    except Exception as e:
        logger.exception("next_question 失败（并行模式）: %s", e)

    try:
        await eval_done_task
    except Exception:
        pass


# ---------- WebSocket 入口 ----------


# v0.4：``volc_voice_key`` 是新合同的唯一语音业务字段；旧 ``llm_key`` /
# ``dashscope_key`` / ``voice_*_token`` 也仍属敏感字段，必须走 auth 首帧而
# 非 URL query。命中 ``_SENSITIVE_QS_KEYS`` 的旧客户端会被 warning 并忽略
# 该 key（既不引发兼容性破坏，也不让密钥意外飘到访问日志/HTTP 历史）。
_SENSITIVE_QS_KEYS = (
    "llm_key",
    "dashscope_key",
    "voice_token",
    "voice_tts_token",
    "voice_stt_token",
    "voice_key",
    "volc_voice_key",
)


@router.websocket("/ws/interview/{sid}")
async def interview_ws(
    ws: WebSocket,
    sid: str,
    llm_provider: str = Query(default=""),
    llm_model: str = Query(default=""),
    llm_model_fast: str = Query(default=""),
    llm_model_deep: str = Query(default=""),
) -> None:
    qs_lower = (ws.url.query or "").lower()
    leaked_qs = [k for k in _SENSITIVE_QS_KEYS if (k + "=") in qs_lower]
    if leaked_qs:
        logger.warning(
            "deprecated sensitive WS query params ignored: %s (client=%s)",
            ",".join(leaked_qs),
            ws.client,
        )

    await ws.accept()
    session_row = await _load_session(sid)
    if session_row is None:
        await ws.send_text(json.dumps({"type": "error", "message": "面试不存在"}))
        await ws.close()
        return

    # 等待 auth 首帧：超时 10s。若首帧不是 auth（老的纯环境变量集成测试），
    # 把它缓存到 pending_first_raw，由下方主循环重放，凭据则全空 → 业务侧
    # 走 has_voice=False 降级路径。
    auth_payload: dict[str, Any] = {}
    pending_first_raw: str | None = None
    try:
        first_raw = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
    except asyncio.TimeoutError:
        await ws.send_text(json.dumps({"type": "error", "message": "auth 超时"}))
        await ws.close()
        return
    except WebSocketDisconnect:
        return
    else:
        try:
            first_msg = json.loads(first_raw)
        except Exception:
            first_msg = None
        if isinstance(first_msg, dict) and first_msg.get("type") == "auth":
            auth_payload = first_msg
        else:
            pending_first_raw = first_raw

    llm_creds = llm_creds_from_query(
        provider=str(auth_payload.get("llm_provider") or "")
        or llm_provider
        or session_row.llm_provider,
        key=str(auth_payload.get("llm_key") or ""),
        model=str(auth_payload.get("llm_model") or "")
        or llm_model
        or session_row.llm_model,
        model_fast=str(auth_payload.get("llm_model_fast") or "") or llm_model_fast,
        model_deep=str(auth_payload.get("llm_model_deep") or "") or llm_model_deep,
    )
    voice_creds = voice_creds_from_query(
        voice_key=str(auth_payload.get("volc_voice_key") or ""),
    )
    # #region agent log
    try:
        _has_voice = voice_creds.has_voice_creds()
        _log_path = os.environ.get(
            "QI_DEBUG_LOG",
            (Path(__file__).resolve().parent.parent.parent.parent / "debug-ef57b3.log").as_posix(),
        )
        with open(_log_path, "a", encoding="utf-8") as _fh:
            _fh.write(
                json.dumps(
                    {
                        "sessionId": "ef57b3",
                        "timestamp": int(time.time() * 1000),
                        "location": "voice_ws.interview_ws:voice_creds",
                        "message": "WS voice creds (no secrets)",
                        "data": {
                            "has_volc_voice_key": _has_voice,
                            "key_len": len(voice_creds.voice_key_effective()),
                            "llm_fast": (llm_creds.model_fast or ""),
                            "llm_deep": (llm_creds.model_deep or ""),
                        },
                        "hypothesisId": "P5-VOICE-VOLC",
                        "runId": "post-stream",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass
    # #endregion

    engine = InterviewerEngine(
        interview_type=session_row.interview_type,
        job_title=session_row.job_title,
        job_jd=session_row.job_jd,
        resume_text=session_row.resume_text,
        initial_score=session_row.initial_score,
        creds=llm_creds,
    )

    ctx = _SessionContext(
        ws,
        sid,
        engine,
        voice_creds,
        _safe_speaker(session_row.voice_speaker, session_row.interview_type),
    )

    for t in session_row.turns:
        engine.history.append(
            TurnRecord(
                idx=t.idx,
                role=t.role,
                text=t.text,
                strategy=t.strategy,
                expected_topic=t.expected_topic,
                score_delta=t.score_delta,
                score_after=t.score_after,
                evaluator_json=t.evaluator_json,
            )
        )
        if t.role == "candidate":
            engine.current_score = t.score_after

    try:
        while True:
            if pending_first_raw is not None:
                raw = pending_first_raw
                pending_first_raw = None
            else:
                raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype = msg.get("type")

            if mtype == "auth":
                continue

            if mtype == "start":
                _qidbg(
                    "voice_ws.main_loop:start",
                    "received start, scheduling opening pipeline",
                    {"sid": sid, "has_voice": ctx.has_voice},
                )
                asyncio.create_task(_opening_pipeline(ctx), name="opening")

            elif mtype == "audio_chunk":
                b64 = msg.get("pcm_base64") or ""
                if b64:
                    await ctx.ensure_stt()
                    try:
                        pcm = base64.b64decode(b64)
                    except Exception:
                        pcm = b""
                    if pcm:
                        await ctx.feed_audio(pcm)

            elif mtype == "user_interrupt":
                await ctx.cancel_tts()

            elif mtype == "client_replay_tts":
                # i12 / i14 — 语音手动化（v0.2）：前端用户点 [朗读] 按钮
                # 触发；把整段文本一次性塞进 TTS 队列，走与 opening / next_question
                # 完全相同的 ``play_text_stream`` 路径，仅触发源不同。
                replay_text = (msg.get("text") or "").strip()
                _qidbg(
                    "voice_ws.main_loop:client_replay_tts",
                    "received client_replay_tts",
                    {
                        "sid": sid,
                        "text_len": len(replay_text),
                        "text_head": replay_text[:64],
                        "has_voice": ctx.has_voice,
                    },
                )
                if not replay_text:
                    continue
                # 上一段还在播 → 先打断（用户重复点 [朗读] 时不堆叠）。
                await ctx.cancel_tts()

                async def _drive_replay(text: str = replay_text) -> None:
                    queue: asyncio.Queue[str | None] = asyncio.Queue()
                    await queue.put(text)
                    await queue.put(None)
                    _qidbg(
                        "voice_ws.client_replay_tts:enqueued",
                        "enqueued full text + sentinel into TTS queue",
                        {"sid": sid, "text_len": len(text)},
                    )
                    try:
                        await ctx.play_text_stream(queue)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        _qidbg(
                            "voice_ws.client_replay_tts:fail",
                            "play_text_stream raised",
                            {"sid": sid, "err": f"{type(exc).__name__}: {exc}"[:200]},
                        )

                ctx.tts_play_task = asyncio.create_task(
                    _drive_replay(), name="client-replay-tts"
                )

            elif mtype == "end_turn":
                _qidbg(
                    "voice_ws.main_loop:end_turn",
                    "received end_turn from client",
                    {
                        "sid": sid,
                        "has_voice": ctx.has_voice,
                        "has_stt": ctx.stt_session is not None,
                        "lock_busy": ctx.processing_lock.locked(),
                        "last_partial_len": len(ctx.last_partial_text or ""),
                        "fallback_len": len((msg.get("fallback_text") or "").strip()),
                    },
                )
                if ctx.stt_session is not None:
                    try:
                        await ctx.stt_session.finish()
                    except Exception:
                        pass
                fallback = (msg.get("fallback_text") or "").strip()
                if not fallback and ctx.has_voice:
                    asyncio.create_task(
                        _maybe_finalize_with_partial(ctx),
                        name="final-partial-fallback",
                    )
                elif fallback:
                    asyncio.create_task(
                        ctx._on_user_final(fallback), name="final-fallback"
                    )

            elif mtype == "answer_text":
                text = (msg.get("text") or "").strip()
                _qidbg(
                    "voice_ws.main_loop:answer_text",
                    "received answer_text",
                    {
                        "sid": sid,
                        "text_len": len(text),
                        "ended": ctx.ended,
                        "has_voice": ctx.has_voice,
                    },
                )
                if not text:
                    continue
                # i16 / v0.3：``answer_text`` 路径下，前端在发送时已 setTextAnswer("")
                # 清空 textarea。回推 stt_final 会让前端再 setTextAnswer(text) 把
                # 刚 clear 的 textarea 又填回去 → UX 混乱（用户以为消息没发出去）。
                asyncio.create_task(
                    ctx._on_user_final(text, emit_stt_final=False),
                    name="final-text",
                )

            elif mtype == "end_interview":
                await ctx.cancel_tts()
                await _persist_final(ctx.sid, ctx.engine.current_score, "user")
                await ctx.send_json({"type": "interview_end", "reason": "user"})
                ctx.ended = True
                break

            else:
                pass

            if ctx.ended:
                break

    except WebSocketDisconnect:
        logger.info("Client disconnected: %s", sid)
    except Exception as e:
        logger.exception("WS error: %s", e)
        try:
            await ctx.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        await ctx.cancel_tts()
        if ctx.stt_consumer_task and not ctx.stt_consumer_task.done():
            ctx.stt_consumer_task.cancel()
            try:
                await ctx.stt_consumer_task
            except (asyncio.CancelledError, Exception):
                pass
        if ctx.stt_session is not None:
            try:
                await ctx.stt_session.close()
            except Exception:
                pass
        try:
            await ws.close()
        except Exception:
            pass
