"""面试官引擎：状态机 + 追问决策 + 打断 + 熔断。

提供两种调用形态：
- ``opening / next_question / wrap_up / interrupt_speech``：一次性等全文（旧路径，
  用于测试 / mock-LLM 模式 / 兜底）
- ``stream_opening / stream_next_question / stream_wrap_up / stream_interrupt_speech``：
  流式吐字（用于驱动 TTS continue-task），yield 形式：
    {"type": "speech_chunk", "text": "..."}
    {"type": "speech_done"}
    {"type": "done", "data": {...meta...}}
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from app.config import get_settings
from app.core.credentials import LLMCreds
from app.services.llm import (
    chat_complete,
    render_prompt,
    safe_parse_json,
    stream_speech_then_meta,
)
from app.services.scoring import is_break_threshold, normalize_evaluator


InterviewStateName = Literal["greeting", "self_intro", "probing", "wrap_up", "ended"]

INTERVIEW_TYPE_LABEL = {
    "tech1": "技术一面",
    "tech2": "技术二面",
    "comprehensive": "综合面",
    "hr": "HR 面",
}

MAX_ROUNDS = 8  # 防止无限追问


@dataclass
class TurnRecord:
    idx: int
    role: str  # interviewer | candidate
    text: str
    strategy: str | None = None
    expected_topic: str | None = None
    score_delta: int = 0
    score_after: int = 0
    evaluator_json: dict[str, Any] | None = None


@dataclass
class InterviewerEngine:
    interview_type: str
    job_title: str
    job_jd: str
    resume_text: str
    initial_score: int
    creds: LLMCreds

    history: list[TurnRecord] = field(default_factory=list)
    current_score: int = 0
    state: InterviewStateName = "greeting"
    last_expected_topic: str = ""
    last_question: str = ""

    def __post_init__(self) -> None:
        if self.current_score == 0:
            self.current_score = self.initial_score

    @property
    def type_label(self) -> str:
        return INTERVIEW_TYPE_LABEL.get(self.interview_type, "面试")

    def _system_prompt(self) -> str:
        return render_prompt(
            "system_interviewer.j2",
            interview_type=self.interview_type,
            interview_type_label=self.type_label,
        )

    # ----------------- 一次性入口（保留向后兼容） -----------------

    async def opening(self) -> dict[str, Any]:
        prompt = render_prompt(
            "opening.j2",
            interviewer_name="李老师",
            interview_type_label=self.type_label,
            job_title=self.job_title or "（未指定）",
            job_jd=self.job_jd or "",
        )
        raw = await chat_complete(
            self.creds,
            messages=[
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": prompt},
            ],
            temperature=0.6,
            max_tokens=400,
            response_format_json=True,
            tier="fast",
        )
        data = safe_parse_json(raw)
        speech = data.get("speech") or "你好，我是今天的面试官，请你先做一个 1 分钟左右的自我介绍吧。"
        self._record_opening(speech)
        return {"speech": speech, "strategy": "opening"}

    async def next_question(self) -> dict[str, Any]:
        s = get_settings()
        history_for_prompt = [
            {"idx": h.idx, "role": h.role, "text": h.text} for h in self.history[-10:]
        ]
        prompt = render_prompt(
            "round_question.j2",
            resume_text=self.resume_text or "",
            job_title=self.job_title or "",
            job_jd=self.job_jd or "",
            history=history_for_prompt,
            current_score=self.current_score,
            threshold=s.score_threshold_break,
            max_rounds=MAX_ROUNDS,
        )
        raw = await chat_complete(
            self.creds,
            messages=[
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=500,
            response_format_json=True,
            tier="fast",
        )
        data = safe_parse_json(raw)
        strategy = data.get("strategy", "breadth")
        speech = data.get("speech") or "请你简单聊一下你最熟悉的一个项目。"
        expected = data.get("expected_topic", "")

        rounds_done = sum(
            1 for h in self.history if h.role == "interviewer" and h.strategy != "opening"
        )
        if rounds_done >= MAX_ROUNDS or is_break_threshold(self.current_score):
            return await self.wrap_up(
                reason="score_threshold" if is_break_threshold(self.current_score) else "complete"
            )

        self._record_question(speech, strategy, expected)
        return {"speech": speech, "strategy": strategy, "expected_topic": expected}

    def append_candidate_turn(self, answer: str) -> TurnRecord:
        """**先**把候选人 turn 加进 history（占位评分），让随后的
        ``stream_next_question`` 能看到候选人最新一句话作为上下文，
        而不必等 ``evaluate_answer`` 跑完才知道有这一轮。

        D12 修复：原来 ``_process_answer`` 是串行 evaluate（~9s）→ next_question
        （~10s LLM TTFT）→ TTS。把这两步并行后必须保证 next_question 看到
        candidate turn，所以拆出这个轻量方法，由调用方先调它，再并行触发
        ``apply_evaluation`` 与 ``stream_next_question``。
        """
        rec = TurnRecord(
            idx=len(self.history) + 1,
            role="candidate",
            text=answer,
            score_delta=0,
            score_after=self.current_score,
            evaluator_json=None,
        )
        self.history.append(rec)
        return rec

    async def evaluate_answer(self, answer: str) -> dict[str, Any]:
        """兼容旧路径：append turn + evaluate + apply。新代码请用
        ``append_candidate_turn`` + ``evaluate_existing_turn`` 以并行化。"""
        rec = self.append_candidate_turn(answer)
        return await self.evaluate_existing_turn(rec, answer)

    async def evaluate_existing_turn(
        self, turn: TurnRecord, answer: str
    ) -> dict[str, Any]:
        """对一条已经在 history 里的 candidate turn 做评分，**就地更新**它。"""
        prompt = render_prompt(
            "evaluator.j2",
            question=self.last_question,
            expected_topic=self.last_expected_topic or "通用",
            answer=answer,
        )
        raw = await chat_complete(
            self.creds,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=600,
            response_format_json=True,
            tier="fast",
        )
        normalized = normalize_evaluator(safe_parse_json(raw))
        delta = int(normalized["delta"])
        self.current_score = max(0, min(100, self.current_score + delta))
        turn.score_delta = delta
        turn.score_after = self.current_score
        turn.evaluator_json = normalized
        return {"delta": delta, "score": self.current_score, "evaluator": normalized}

    async def interrupt_speech(self, reason: str) -> str:
        prompt = render_prompt("interrupt.j2", reason=reason)
        text = await chat_complete(
            self.creds,
            messages=[
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": prompt},
            ],
            temperature=0.6,
            max_tokens=120,
            tier="fast",
        )
        return text.strip().strip('"').strip("「」")

    async def wrap_up(self, *, reason: str) -> dict[str, Any]:
        prompt = render_prompt("wrap_up.j2", reason=reason)
        raw = await chat_complete(
            self.creds,
            messages=[
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=200,
            response_format_json=True,
            tier="fast",
        )
        data = safe_parse_json(raw)
        speech = data.get("speech") or "今天的提问就到这里，感谢你的时间，后续 HR 会和你同步进展。"
        self._record_wrap(speech, reason)
        return {"speech": speech, "strategy": "wrap_up", "end_reason": reason}

    # ----------------- 流式入口 -----------------

    async def stream_opening(self) -> AsyncIterator[dict[str, Any]]:
        """开场白：走"模板直出"路径，绕过 LLM。

        D12 修复（VOICE-E2E-LATENCY）：
        实测发现豆包 lite 模型在该网络环境下 ``client.chat.completions.create``
        要 7+ 秒才返回首个 delta（API 排队 / 火山方舟首字延迟），且开场白本身
        高度模板化（问候 + 自介 + 邀请自我介绍），不需要"真智能"。直接用
        Jinja 风格字符串拼装，再按子句切片成 ``speech_chunk`` 喂给 TTS，
        把 i7（"进入面试 → 首字音频"）从 ~9 s 拉到 ~1.5 s。

        次轮提问 (``stream_next_question``) 仍走 LLM，因为它需要根据简历
        和上下文动态出题。
        """
        job_label = (self.job_title or "").strip()
        job_clause = f"针对 {job_label} 这个岗位，" if job_label else ""
        speech = (
            f"你好，我是李老师，{self.type_label}面试官。"
            f"{job_clause}今天我们就先开始吧，"
            f"请你做一个一分钟以内的自我介绍。"
        )

        # 把整段切成 ~12-18 字的子句块，模拟 LLM 流式吐字给 TTS continue-task。
        # 标点（。，等）已被 ``stream_speech_then_meta`` 的切分逻辑视为强/弱触发，
        # 因此此处直接按强标点切，行为与真 LLM 流近似。
        chunks: list[str] = []
        buf = ""
        for ch in speech:
            buf += ch
            if ch in "，。！？；":
                chunks.append(buf)
                buf = ""
        if buf:
            chunks.append(buf)

        for piece in chunks:
            yield {"type": "speech_chunk", "text": piece}
        yield {"type": "speech_done"}
        self._record_opening(speech)
        yield {"type": "done", "data": {"speech": speech, "strategy": "opening"}}

    async def stream_next_question(self) -> AsyncIterator[dict[str, Any]]:
        s = get_settings()
        history_for_prompt = [
            {"idx": h.idx, "role": h.role, "text": h.text} for h in self.history[-10:]
        ]
        prompt = render_prompt(
            "round_question.j2",
            resume_text=self.resume_text or "",
            job_title=self.job_title or "",
            job_jd=self.job_jd or "",
            history=history_for_prompt,
            current_score=self.current_score,
            threshold=s.score_threshold_break,
            max_rounds=MAX_ROUNDS,
        )

        rounds_done = sum(
            1 for h in self.history if h.role == "interviewer" and h.strategy != "opening"
        )
        if rounds_done >= MAX_ROUNDS or is_break_threshold(self.current_score):
            async for ev in self.stream_wrap_up(
                reason="score_threshold" if is_break_threshold(self.current_score) else "complete"
            ):
                yield ev
            return

        speech_acc: list[str] = []
        meta: dict[str, Any] = {}
        async for ev in stream_speech_then_meta(
            self.creds,
            messages=[
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=500,
            tier="fast",
            json_response=True,
        ):
            if ev["type"] == "speech_chunk":
                speech_acc.append(ev["text"])
                yield ev
            elif ev["type"] == "speech_done":
                yield ev
            elif ev["type"] == "done":
                meta = ev.get("data") or {}

        speech = "".join(speech_acc).strip() or str(
            meta.get("speech") or "请你简单聊一下你最熟悉的一个项目。"
        )
        strategy = str(meta.get("strategy") or "breadth")
        expected = str(meta.get("expected_topic") or "")
        self._record_question(speech, strategy, expected)
        yield {
            "type": "done",
            "data": {
                "speech": speech,
                "strategy": strategy,
                "expected_topic": expected,
            },
        }

    async def stream_wrap_up(self, *, reason: str) -> AsyncIterator[dict[str, Any]]:
        prompt = render_prompt("wrap_up.j2", reason=reason)
        speech_acc: list[str] = []
        meta: dict[str, Any] = {}
        async for ev in stream_speech_then_meta(
            self.creds,
            messages=[
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=200,
            tier="fast",
            json_response=True,
        ):
            if ev["type"] == "speech_chunk":
                speech_acc.append(ev["text"])
                yield ev
            elif ev["type"] == "speech_done":
                yield ev
            elif ev["type"] == "done":
                meta = ev.get("data") or {}

        speech = "".join(speech_acc).strip() or str(
            meta.get("speech")
            or "今天的提问就到这里，感谢你的时间，后续 HR 会和你同步进展。"
        )
        self._record_wrap(speech, reason)
        yield {
            "type": "done",
            "data": {"speech": speech, "strategy": "wrap_up", "end_reason": reason},
        }

    async def stream_interrupt_speech(self, reason: str) -> AsyncIterator[dict[str, Any]]:
        prompt = render_prompt("interrupt.j2", reason=reason)
        speech_acc: list[str] = []
        async for ev in stream_speech_then_meta(
            self.creds,
            messages=[
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": prompt},
            ],
            temperature=0.6,
            max_tokens=120,
            tier="fast",
            json_response=False,
        ):
            if ev["type"] == "speech_chunk":
                speech_acc.append(ev["text"])
                yield ev
            elif ev["type"] == "speech_done":
                yield ev
            elif ev["type"] == "done":
                speech = (
                    "".join(speech_acc)
                    .strip()
                    .strip('"')
                    .strip("「」")
                    or "稍等，我们先聚焦在原问题上。"
                )
                yield {
                    "type": "done",
                    "data": {"speech": speech, "strategy": "interrupt"},
                }

    # ----------------- 内部 record helpers -----------------

    def _record_opening(self, speech: str) -> None:
        self.history.append(
            TurnRecord(
                idx=len(self.history) + 1,
                role="interviewer",
                text=speech,
                strategy="opening",
                score_after=self.current_score,
            )
        )
        self.last_question = speech
        self.state = "self_intro"

    def _record_question(self, speech: str, strategy: str, expected: str) -> None:
        self.last_question = speech
        self.last_expected_topic = expected
        self.history.append(
            TurnRecord(
                idx=len(self.history) + 1,
                role="interviewer",
                text=speech,
                strategy=strategy,
                expected_topic=expected,
                score_after=self.current_score,
            )
        )
        self.state = "probing"

    def _record_wrap(self, speech: str, reason: str) -> None:
        self.history.append(
            TurnRecord(
                idx=len(self.history) + 1,
                role="interviewer",
                text=speech,
                strategy="wrap_up",
                expected_topic=reason,
                score_after=self.current_score,
            )
        )
        self.state = "wrap_up"
