"""评分系统：印象分 + 动态加减分 + 熔断阈值。"""
from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.core.credentials import LLMCreds
from app.services.llm import chat_complete, render_prompt, safe_parse_json


_DIM_KEYS = ("education", "experience", "projects", "papers", "match")


def _normalize_breakdown(raw_breakdown: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """规整 breakdown：保证 5 个维度齐全，score 在 1-10。"""
    breakdown: dict[str, dict[str, Any]] = {}
    src = raw_breakdown or {}
    for k in _DIM_KEYS:
        item = src.get(k) if isinstance(src.get(k), dict) else {}
        try:
            score = int(item.get("score", 5))
        except (TypeError, ValueError):
            score = 5
        score = max(1, min(10, score))
        reason = str(item.get("reason") or "信息不足，无法判断。")
        breakdown[k] = {"score": score, "reason": reason}
    return breakdown


async def compute_initial_score(
    creds: LLMCreds,
    *,
    resume_text: str,
    job_title: str,
    job_jd: str,
) -> tuple[int, str, dict[str, Any]]:
    """返回 (score, one-liner reason, breakdown[dim->{score,reason}])."""
    s = get_settings()
    prompt = render_prompt(
        "initial_score.j2",
        resume_text=resume_text or "",
        job_title=job_title or "",
        job_jd=job_jd or "",
        min_score=s.initial_score_min,
        max_score=s.initial_score_max,
    )
    try:
        raw = await chat_complete(
            creds,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=600,
            response_format_json=True,
            tier="fast",
        )
        data = safe_parse_json(raw)
        score = int(data.get("score", 0))
        reason = str(data.get("reason", ""))
        score = max(s.initial_score_min, min(s.initial_score_max, score))
        breakdown = _normalize_breakdown(data.get("breakdown"))
        return score, reason, breakdown
    except Exception as e:
        # 兜底：均值评分 + 占位 breakdown
        fallback_score = (s.initial_score_min + s.initial_score_max) // 2
        fb_breakdown = _normalize_breakdown(None)
        return (
            fallback_score,
            f"LLM 调用失败，使用兜底分：{e}",
            fb_breakdown,
        )


def clamp_score(score: int) -> int:
    return max(0, min(100, score))


def is_break_threshold(score: int) -> bool:
    return score < get_settings().score_threshold_break


def normalize_evaluator(payload: dict[str, Any]) -> dict[str, Any]:
    """规整 evaluator 返回：保证字段齐全且 delta 在 [-15, 15]。"""
    delta_raw = payload.get("delta", 0)
    try:
        delta = int(delta_raw)
    except (TypeError, ValueError):
        delta = 0
    delta = max(-15, min(15, delta))
    return {
        "scores": payload.get("scores", {}) or {},
        "delta": delta,
        "off_topic": bool(payload.get("off_topic", False)),
        "too_long": bool(payload.get("too_long", False)),
        "strengths": str(payload.get("strengths", "")),
        "weaknesses": str(payload.get("weaknesses", "")),
        "reference": str(payload.get("reference", "")),
    }
