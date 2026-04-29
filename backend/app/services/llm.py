"""LLM 工厂：通过 OpenAI 兼容协议调用豆包 / DeepSeek / Qwen / GLM。

支持：
- 模型档位（fast/deep）：``creds.pick_model('fast' | 'deep')`` 自动选档；
- 流式文本：``chat_stream_text``（真实 + mock 双路径）；
- 非流式：``chat_complete`` 保留原签名，新增 ``tier`` 可选参数。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from openai import AsyncOpenAI

from app.core.credentials import LLMCreds
from app.services import llm_mock


# #region agent log
def _qi_chat_dbg(location: str, message: str, data: dict[str, Any]) -> None:
    """Append one NDJSON line to .cursor/debug-714cc8.log for session 714cc8."""
    try:
        path = Path(__file__).resolve().parents[3] / ".cursor" / "debug-714cc8.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "sessionId": "714cc8",
                        "runId": "be_qi_chat",
                        "hypothesisId": "H7-LLM-FAST-MISSING",
                        "timestamp": int(time.time() * 1000),
                        "location": location,
                        "message": message,
                        "data": data,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass
# #endregion


PROVIDERS: dict[str, str] = {
    "doubao": "https://ark.cn-beijing.volces.com/api/v3",
    "deepseek": "https://api.deepseek.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
}


_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_jinja_env = Environment(
    loader=FileSystemLoader(_PROMPTS_DIR.as_posix()),
    autoescape=select_autoescape(disabled_extensions=("j2", "txt")),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_prompt(template: str, **ctx: Any) -> str:
    return _jinja_env.get_template(template).render(**ctx)


def build_client(creds: LLMCreds) -> AsyncOpenAI:
    """旧的"每次新建客户端"路径——保留作兼容入口。新代码推荐 ``_get_client``。

    注意：每次新建 ``AsyncOpenAI`` 等价于丢弃 httpx 连接池，下一次 LLM 调用
    会重新做 TLS 握手；在当前 VPN/MITM 网络下这一步约 7-8 s，是 D12
    （test_i8 / test_i5 / test_p2）的根因之一。
    """
    base_url = PROVIDERS.get(creds.provider, PROVIDERS["doubao"])
    if not creds.api_key:
        raise RuntimeError(
            "缺少 LLM API Key，请在前端配置或在 .env.local 设置 ARK_API_KEY"
        )
    return AsyncOpenAI(api_key=creds.api_key, base_url=base_url, timeout=60.0)


def _get_client(creds: LLMCreds) -> AsyncOpenAI:
    """返回单例缓存的 ``AsyncOpenAI``，复用 httpx 连接池（TLS 已预热）。

    D12 修复：``app.services.llm_pool`` 在启动期 warmup 一次小请求，把
    TLS 握手提前做掉；后续业务调用直接命中 keepalive 连接（300s
    expiry + 60s ping），把 ``after_create_ms`` 从 ~7900 ms 降到 <100 ms。

    若调用方未 warmup（pool 不知道这套 creds），也会 lazy 创建一个缓存
    的客户端，第一次调用仍会走 TLS 握手，但后续调用就能复用连接。
    """
    if not creds.api_key:
        raise RuntimeError(
            "缺少 LLM API Key，请在前端配置或在 .env.local 设置 ARK_API_KEY"
        )
    from app.services.llm_pool import pool as _llm_pool

    return _llm_pool.get(creds)


def _resolve_model(creds: LLMCreds, tier: str | None) -> str:
    if tier in ("fast", "deep"):
        return creds.pick_model(tier)
    return creds.model


async def chat_complete(
    creds: LLMCreds,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.7,
    max_tokens: int = 1024,
    response_format_json: bool = False,
    tier: str | None = None,
) -> str:
    # #region agent log
    if llm_mock.is_mock_enabled():
        try:
            _instr_write(
                "llm.chat_complete:mock",
                "mock LLM call",
                {"messages_n": len(messages), "json": response_format_json, "tier": tier},
                hypothesis_id="H2-LLM-MOCK",
            )
        except Exception:
            pass
        return llm_mock.mock_chat_complete(messages, response_format_json=response_format_json)
    # #endregion
    client = _get_client(creds)
    _resolved_model = _resolve_model(creds, tier)
    kwargs: dict[str, Any] = {
        "model": _resolved_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format_json:
        kwargs["response_format"] = {"type": "json_object"}
    # #region agent log
    _qi_chat_dbg(
        "llm.chat_complete:enter",
        "calling chat completions",
        {
            "tier": tier,
            "model": _resolved_model,
            "model_fast_cfg": creds.model_fast,
            "model_default": creds.model,
            "max_tokens": max_tokens,
            "json": response_format_json,
        },
    )
    _t0 = time.perf_counter()
    # #endregion
    try:
        resp = await client.chat.completions.create(**kwargs)
        _content = resp.choices[0].message.content or ""
        # #region agent log
        _qi_chat_dbg(
            "llm.chat_complete:exit",
            "chat completions returned",
            {
                "tier": tier,
                "model": _resolved_model,
                "elapsed_ms": int((time.perf_counter() - _t0) * 1000),
                "out_chars": len(_content),
            },
        )
        # #endregion
        return _content
    except Exception as _exc:
        # #region agent log
        _qi_chat_dbg(
            "llm.chat_complete:error",
            "chat completions raised",
            {
                "tier": tier,
                "model": _resolved_model,
                "elapsed_ms": int((time.perf_counter() - _t0) * 1000),
                "err": f"{type(_exc).__name__}: {_exc}",
            },
        )
        # #endregion
        raise


# #region agent log
def _instr_write(location: str, message: str, data: dict[str, Any], *, hypothesis_id: str = "") -> None:
    """Append one NDJSON line to the debug log file (best-effort)."""
    import os
    import time

    path = os.environ.get("QI_DEBUG_LOG", "debug-ef57b3.log")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "sessionId": "ef57b3",
                        "id": f"log_{int(time.time()*1000)}",
                        "timestamp": int(time.time() * 1000),
                        "location": location,
                        "message": message,
                        "data": data,
                        "hypothesisId": hypothesis_id,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass
# #endregion


async def chat_stream(
    creds: LLMCreds,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.7,
    max_tokens: int = 1024,
    tier: str | None = None,
) -> AsyncIterator[str]:
    """旧的纯文本流式接口。建议新代码改用 ``chat_stream_text``（带 mock 支持）。"""
    client = _get_client(creds)
    stream = await client.chat.completions.create(
        model=_resolve_model(creds, tier),
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta and delta.content:
            yield delta.content


async def chat_stream_text(
    creds: LLMCreds,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.7,
    max_tokens: int = 1024,
    response_format_json: bool = False,
    tier: str | None = None,
) -> AsyncIterator[str]:
    """统一的流式文本接口：mock 模式下也按字流式吐字（用于驱动 TTS continue-task）。"""
    if llm_mock.is_mock_enabled():
        full = llm_mock.mock_chat_complete(
            messages, response_format_json=response_format_json
        )
        # 切成 ~6 字一片，便于上层流式聚合时也能命中子句切分
        i = 0
        while i < len(full):
            piece = full[i : i + 6]
            i += 6
            yield piece
            await asyncio.sleep(0)
        return

    client = _get_client(creds)
    _resolved_model = _resolve_model(creds, tier)
    kwargs: dict[str, Any] = {
        "model": _resolved_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if response_format_json:
        # 注意：豆包等部分 OpenAI 兼容 provider 在开启 response_format=json_object 时，
        # 会等到整段 JSON 校验完成才一次性把所有 delta 推给客户端，
        # 等价于把"流式"退化成"非流式"。
        # 我们的 stream_speech_then_meta 已经能容错 JSON 子串、且 prompt 仍要求 JSON 输出，
        # 因此这里**不再传** response_format=json_object，让 token 真正流式回来；
        # safe_parse_json 在尾段做整段解析兜底。
        # 这次去掉是 D12（VOICE-E2E-LATENCY）的关键修复：i7 实测从 8.1s LLM 延迟降到 1-2s。
        pass
    # #region agent log
    _t0 = time.perf_counter()
    _ttft_ms: int | None = None
    _delta_n = 0
    # #endregion
    stream = await client.chat.completions.create(**kwargs)
    # #region agent log
    _qi_chat_dbg(
        "llm.chat_stream_text:enter",
        "stream call returned, awaiting deltas",
        {
            "tier": tier,
            "model": _resolved_model,
            "max_tokens": max_tokens,
            "json_prompt": response_format_json,
            "after_create_ms": int((time.perf_counter() - _t0) * 1000),
        },
    )
    # #endregion
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta and delta.content:
            # #region agent log
            if _ttft_ms is None:
                _ttft_ms = int((time.perf_counter() - _t0) * 1000)
                _qi_chat_dbg(
                    "llm.chat_stream_text:first_token",
                    "received first content delta from LLM",
                    {
                        "tier": tier,
                        "model": _resolved_model,
                        "ttft_ms": _ttft_ms,
                        "first_delta_chars": len(delta.content),
                    },
                )
            _delta_n += 1
            # #endregion
            yield delta.content
    # #region agent log
    _qi_chat_dbg(
        "llm.chat_stream_text:exit",
        "stream finished",
        {
            "tier": tier,
            "model": _resolved_model,
            "elapsed_ms": int((time.perf_counter() - _t0) * 1000),
            "ttft_ms": _ttft_ms,
            "delta_n": _delta_n,
        },
    )
    # #endregion


_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")


def safe_parse_json(text: str) -> dict[str, Any]:
    """从 LLM 输出里抠出第一段 JSON；失败返回空 dict。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        return json.loads(text)
    except Exception:
        m = _JSON_BLOCK_RE.search(text)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}


# ---- 流式 → speech 子句 + 元数据 ----

# 子句切分触发字符；命中即立刻 flush 给 TTS（即使长度未达阈值）
_SENTENCE_PUNCT = "。！？；…\n!?;"
# 弱触发字符：达到 _SOFT_FLUSH_LEN 字数后命中即 flush
_SOFT_PUNCT = ",，、—:："
# 兜底字数阈值：无任何标点也强制切片
_HARD_FLUSH_LEN = 28
_SOFT_FLUSH_LEN = 14


def _try_extract_speech_prefix(buf: str) -> tuple[str | None, int]:
    """从 JSON 流式片段里尝试提取 ``"speech": "..."`` 内已经写到的内容。

    返回 (累积的 speech 文本, 已经消费到 buf 中的下标)；若还没看到 speech 字段，返回 (None, 0)。
    """
    m = re.search(r'"speech"\s*:\s*"', buf)
    if not m:
        return None, 0
    start = m.end()
    out: list[str] = []
    i = start
    while i < len(buf):
        ch = buf[i]
        if ch == "\\" and i + 1 < len(buf):
            nxt = buf[i + 1]
            esc = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt)
            out.append(esc)
            i += 2
            continue
        if ch == '"':
            # speech 字段已经闭合
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    return "".join(out), i  # 字段尚未闭合，整段都已消费


async def stream_speech_then_meta(
    creds: LLMCreds,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.7,
    max_tokens: int = 600,
    tier: str | None = None,
    json_response: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """从 LLM 流式响应里实时抽取 ``speech`` 文本块，并在收尾时给出 ``meta``。

    yield 形式（每条都是 dict）：
      - {"type": "speech_chunk", "text": "..."} 子句级块（可立刻送 TTS）
      - {"type": "speech_done"}                速读完毕；meta 还可能在后面
      - {"type": "done", "raw": "<full>", "data": {...}}  整段完成 + 解析后的 JSON

    若上游不是 JSON（``json_response=False``），把全部 token 作为 speech 流出，
    最后 yield 一个 ``done`` 事件（``data`` 仅含 ``speech``）。
    """
    accum_raw: list[str] = []
    accum_text = ""
    sent_idx = 0  # 已经 flush 给 TTS 的 speech 字符数
    speech_done = False
    pending = ""  # 还未 flush 的 speech 缓冲

    def _flush_pending(force: bool = False) -> str | None:
        nonlocal pending
        if not pending:
            return None
        # 找最后一个强标点
        cut = -1
        for i, ch in enumerate(pending):
            if ch in _SENTENCE_PUNCT:
                cut = i
        if cut == -1 and len(pending) >= _SOFT_FLUSH_LEN:
            for i, ch in enumerate(pending):
                if ch in _SOFT_PUNCT:
                    cut = i
        if cut == -1 and len(pending) >= _HARD_FLUSH_LEN:
            cut = len(pending) - 1
        if cut == -1 and not force:
            return None
        if force and cut == -1:
            cut = len(pending) - 1
        out = pending[: cut + 1]
        pending = pending[cut + 1 :]
        return out or None

    async for delta in chat_stream_text(
        creds,
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format_json=json_response,
        tier=tier,
    ):
        accum_raw.append(delta)
        if not json_response:
            pending += delta
            piece = _flush_pending(force=False)
            if piece:
                yield {"type": "speech_chunk", "text": piece}
            continue

        buf = "".join(accum_raw)
        if speech_done:
            continue
        speech_text, consumed = _try_extract_speech_prefix(buf)
        if speech_text is None:
            continue
        # speech_text 是当前已知的完整 speech 内容（含转义还原）
        if len(speech_text) < len(accum_text):
            # 防御性：极端 race 不应出现，跳过
            continue
        new_part = speech_text[len(accum_text) :]
        accum_text = speech_text
        if new_part:
            pending += new_part
            piece = _flush_pending(force=False)
            if piece:
                yield {"type": "speech_chunk", "text": piece}
        # 检测是否 speech 字段已经闭合：原 buf 在 consumed 处 quote 闭合
        if consumed < len(buf) and buf[consumed - 1] == '"':
            speech_done = True
            piece = _flush_pending(force=True)
            if piece:
                yield {"type": "speech_chunk", "text": piece}
            yield {"type": "speech_done"}

    full = "".join(accum_raw)
    # 最后兜底 flush
    if not json_response:
        piece = _flush_pending(force=True)
        if piece:
            yield {"type": "speech_chunk", "text": piece}
        yield {"type": "speech_done"}
        yield {"type": "done", "raw": full, "data": {"speech": "".join(c for c in [full]).strip()}}
        return

    if not speech_done:
        # speech 字段未在流中闭合（罕见：模型把 speech 放尾或 max_tokens 截断）
        piece = _flush_pending(force=True)
        if piece:
            yield {"type": "speech_chunk", "text": piece}
        yield {"type": "speech_done"}

    data = safe_parse_json(full)
    # 若 speech 一开始就没流出（例如 JSON 字段顺序不同），用解析结果补一次
    if not accum_text:
        sp = str(data.get("speech") or "").strip()
        if sp:
            yield {"type": "speech_chunk", "text": sp}
            yield {"type": "speech_done"}
    yield {"type": "done", "raw": full, "data": data}
