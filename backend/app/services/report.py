"""复盘报告生成。

提供两个入口：
- ``build_report``（一次性）：等 LLM 全文返回；用于幂等 GET /reports/{sid} 的回填。
- ``build_report_stream``（流式 SSE）：边 LLM 流式吐字 → 边按字段（summary 先 / strengths 后…）
  emit ``{"section": <field>, "delta": "...", "closed": bool}``，最后 emit
  ``{"section": "done", "data": {...全文 dict...}}``，由调用方负责持久化。
"""
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

from app.core.credentials import LLMCreds
from app.services.llm import (
    chat_complete,
    chat_stream_text,
    render_prompt,
    safe_parse_json,
)


REPORT_FIELDS: tuple[str, ...] = (
    "summary",
    "strengths_md",
    "weaknesses_md",
    "advice_md",
    "score_explanation_md",
)


async def build_report(
    creds: LLMCreds,
    *,
    resume_text: str,
    job_title: str,
    turns: list[dict[str, Any]],
    final_score: int,
    end_reason: str,
    initial_score: int = 0,
    breakdown: dict[str, Any] | None = None,
) -> dict[str, str]:
    prompt = render_prompt(
        "final_report.j2",
        resume_text=resume_text or "",
        job_title=job_title or "",
        turns=turns,
        final_score=final_score,
        end_reason=end_reason or "complete",
        initial_score=initial_score,
        breakdown=breakdown or {},
    )
    raw = await chat_complete(
        creds,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=1800,
        response_format_json=True,
        tier="deep",
    )
    data = safe_parse_json(raw)
    return _normalize_report(data)


def _normalize_report(data: dict[str, Any]) -> dict[str, str]:
    return {f: str(data.get(f, "")) for f in REPORT_FIELDS}


def _try_extract_field(buf: str, field: str) -> tuple[str | None, bool]:
    """从（可能未闭合的）JSON 文本中提取 ``"field": "..."`` 的字符串值。

    返回 (text_so_far, is_closed)。``text_so_far is None`` 表示尚未看到该字段。
    """
    m = re.search(r'"' + re.escape(field) + r'"\s*:\s*"', buf)
    if not m:
        return None, False
    start = m.end()
    out: list[str] = []
    i = start
    while i < len(buf):
        ch = buf[i]
        if ch == "\\" and i + 1 < len(buf):
            nxt = buf[i + 1]
            esc = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "r": "\r"}.get(nxt, nxt)
            out.append(esc)
            i += 2
            continue
        if ch == '"':
            return "".join(out), True
        out.append(ch)
        i += 1
    return "".join(out), False


async def build_report_stream(
    creds: LLMCreds,
    *,
    resume_text: str,
    job_title: str,
    turns: list[dict[str, Any]],
    final_score: int,
    end_reason: str,
    initial_score: int = 0,
    breakdown: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """流式生成复盘报告。

    yield 形式：
      {"type": "section_delta", "section": "summary",      "delta": "...", "closed": False}
      {"type": "section_done",  "section": "summary"}
      ... 其它字段 ...
      {"type": "done", "data": {"summary": "...", ...}, "raw": "<full>"}
    """
    prompt = render_prompt(
        "final_report.j2",
        resume_text=resume_text or "",
        job_title=job_title or "",
        turns=turns,
        final_score=final_score,
        end_reason=end_reason or "complete",
        initial_score=initial_score,
        breakdown=breakdown or {},
    )

    raw_chunks: list[str] = []
    sent_lens: dict[str, int] = {f: 0 for f in REPORT_FIELDS}
    closed_flags: dict[str, bool] = {f: False for f in REPORT_FIELDS}

    async for piece in chat_stream_text(
        creds,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=1800,
        response_format_json=True,
        tier="deep",
    ):
        raw_chunks.append(piece)
        buf = "".join(raw_chunks)
        for f in REPORT_FIELDS:
            if closed_flags[f]:
                continue
            text, closed = _try_extract_field(buf, f)
            if text is None:
                continue
            cur_len = len(text)
            if cur_len > sent_lens[f]:
                delta_text = text[sent_lens[f] :]
                sent_lens[f] = cur_len
                yield {
                    "type": "section_delta",
                    "section": f,
                    "delta": delta_text,
                    "closed": closed,
                }
            if closed:
                closed_flags[f] = True
                yield {"type": "section_done", "section": f}

    full = "".join(raw_chunks)
    data = _normalize_report(safe_parse_json(full))
    # 兜底：流式没识别出来的字段，从最终解析里补发一次
    for f in REPORT_FIELDS:
        if not closed_flags[f] and data.get(f):
            yield {
                "type": "section_delta",
                "section": f,
                "delta": data[f][sent_lens[f] :],
                "closed": True,
            }
            yield {"type": "section_done", "section": f}
    yield {"type": "done", "data": data, "raw": full}
