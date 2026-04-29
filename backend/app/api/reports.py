"""复盘报告 REST。"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth_dep import current_user
from app.core.credentials import LLMCreds, llm_credentials
from app.db.session import AsyncSessionLocal, get_db
from app.models.interview import InterviewSession, Report
from app.models.user import User
from app.schemas.interview import ReportOut, TrendPoint, TurnOut
from app.services.report import build_report, build_report_stream


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/reports", tags=["reports"])


def _trend(turns) -> list[TrendPoint]:
    points: list[TrendPoint] = []
    for t in turns:
        if t.role == "candidate":
            points.append(TrendPoint(idx=t.idx, score=t.score_after, delta=t.score_delta))
    return points


def _turns_for_llm(row: InterviewSession) -> list[dict]:
    return [
        {
            "idx": t.idx,
            "role": t.role,
            "text": t.text,
            "score_delta": t.score_delta,
            "score_after": t.score_after,
        }
        for t in row.turns
    ]


def _impression_dimensions(row: InterviewSession) -> dict:
    impression = row.impression_breakdown or {}
    if isinstance(impression, dict):
        return impression.get("dimensions") or {}
    return {}


@router.get("/{sid}", response_model=ReportOut)
async def get_report(
    sid: str,
    creds: LLMCreds = Depends(llm_credentials),  # noqa: ARG001 - kept for API stability; unused after D4 fix
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> ReportOut:
    """只读取：若已生成 Report 则返回完整内容；否则返回脚手架（trend + turns）。
    LLM 生成由 SSE ``GET /reports/{sid}/stream`` 唯一负责，避免：
      1) 与 SSE 端点 race INSERT 触发 ``UNIQUE constraint failed: report.session_id``；
      2) 同步链路堵塞在 ~60–180 s 的 deep-LLM 调用上，违反 3 s 接口预算。
    """
    stmt = (
        select(InterviewSession)
        .where(InterviewSession.id == sid, InterviewSession.user_id == user.id)
        .options(selectinload(InterviewSession.turns), selectinload(InterviewSession.report))
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "面试不存在")

    if row.report is not None:
        rep = row.report
        return ReportOut(
            session_id=row.id,
            summary=rep.summary,
            strengths_md=rep.strengths_md,
            weaknesses_md=rep.weaknesses_md,
            advice_md=rep.advice_md,
            score_explanation_md=rep.score_explanation_md or "",
            trend=[TrendPoint(**p) for p in (rep.trend_json or [])] or _trend(row.turns),
            turns=[TurnOut.model_validate(t) for t in row.turns],
            initial_score=row.initial_score,
            final_score=row.final_score,
            impression_breakdown=row.impression_breakdown,
            created_at=rep.created_at,
        )

    return ReportOut(
        session_id=row.id,
        summary="",
        strengths_md="",
        weaknesses_md="",
        advice_md="",
        score_explanation_md="",
        trend=_trend(row.turns),
        turns=[TurnOut.model_validate(t) for t in row.turns],
        initial_score=row.initial_score,
        final_score=row.final_score,
        impression_breakdown=row.impression_breakdown,
        created_at=row.created_at,
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get("/{sid}/stream")
async def stream_report(
    sid: str,
    creds: LLMCreds = Depends(llm_credentials),
    user: User = Depends(current_user),
):
    """SSE 流式生成复盘报告。

    若已有缓存（``Report`` 行），直接 emit ``cached`` + ``done`` 然后关流。
    否则边 LLM 流式生成边 emit ``section_delta`` / ``section_done`` / ``done``。
    生成结束后在新 session 持久化到 ``Report`` 表。
    """

    async def event_gen():
        # 1) 取面试 + turns + 既有 report
        try:
            async with AsyncSessionLocal() as db:
                stmt = (
                    select(InterviewSession)
                    .where(
                        InterviewSession.id == sid,
                        InterviewSession.user_id == user.id,
                    )
                    .options(
                        selectinload(InterviewSession.turns),
                        selectinload(InterviewSession.report),
                    )
                )
                row = (await db.execute(stmt)).scalar_one_or_none()
                if not row:
                    yield _sse({"type": "error", "message": "面试不存在"})
                    return
                if row.report is not None:
                    rep = row.report
                    cached = {
                        "summary": rep.summary,
                        "strengths_md": rep.strengths_md,
                        "weaknesses_md": rep.weaknesses_md,
                        "advice_md": rep.advice_md,
                        "score_explanation_md": rep.score_explanation_md or "",
                    }
                    for f, txt in cached.items():
                        if txt:
                            yield _sse({"type": "section_delta", "section": f, "delta": txt, "closed": True})
                            yield _sse({"type": "section_done", "section": f})
                    trend = [
                        p if isinstance(p, dict) else p.model_dump()
                        for p in (rep.trend_json or [_trend(row.turns)])
                    ]
                    yield _sse({"type": "done", "data": cached, "cached": True, "trend": trend})
                    return
                resume_text = row.resume_text
                job_title = row.job_title
                turns_dicts = _turns_for_llm(row)
                final_score = row.final_score
                initial_score = row.initial_score
                end_reason = row.end_reason or "complete"
                breakdown_dimensions = _impression_dimensions(row)
                trend_points = [p.model_dump() for p in _trend(row.turns)]
        except Exception as e:
            logger.exception("SSE report 加载失败 sid=%s", sid)
            yield _sse({"type": "error", "message": f"加载面试失败：{e}"})
            return

        # 2) 流式 LLM
        full_data: dict | None = None
        try:
            async for frag in build_report_stream(
                creds,
                resume_text=resume_text,
                job_title=job_title,
                turns=turns_dicts,
                final_score=final_score,
                end_reason=end_reason,
                initial_score=initial_score,
                breakdown=breakdown_dimensions or {},
            ):
                if frag.get("type") == "done":
                    full_data = frag.get("data") or {}
                    yield _sse(
                        {
                            "type": "done",
                            "data": full_data,
                            "cached": False,
                            "trend": trend_points,
                        }
                    )
                else:
                    yield _sse(frag)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("SSE report 生成失败 sid=%s: %s", sid, e)
            yield _sse({"type": "error", "message": f"报告生成失败：{e}"})
            return

        # 3) 持久化（best-effort，失败不影响已发流）
        # 多个并发 SSE 客户端可能同时落库（React StrictMode 二次挂载、刷新等）。
        # 用 SELECT-then-INSERT + IntegrityError 兜底保证幂等：另一写者已成功就放行。
        if full_data:
            try:
                async with AsyncSessionLocal() as db:
                    existing = await db.get(Report, sid)
                    if existing is None:
                        rep = Report(
                            session_id=sid,
                            summary=full_data.get("summary", ""),
                            strengths_md=full_data.get("strengths_md", ""),
                            weaknesses_md=full_data.get("weaknesses_md", ""),
                            advice_md=full_data.get("advice_md", ""),
                            score_explanation_md=full_data.get(
                                "score_explanation_md", ""
                            ),
                            trend_json=trend_points,
                        )
                        db.add(rep)
                        try:
                            await db.commit()
                        except IntegrityError:
                            # 并发 SSE 已先于本路写入；忽略 UNIQUE 冲突。
                            await db.rollback()
            except IntegrityError:
                pass
            except Exception as e:
                logger.warning("SSE report 持久化失败 sid=%s: %s", sid, e)

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        event_gen(), media_type="text/event-stream", headers=headers
    )


@router.delete("/{sid}")
async def regen_report(
    sid: str,
    creds: LLMCreds = Depends(llm_credentials),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """删除已有报告并强制重生（仅当前用户可操作自己的会话）。"""
    sess = await db.get(InterviewSession, sid)
    if sess is None or sess.user_id != user.id:
        raise HTTPException(404, "面试不存在")
    rep = await db.get(Report, sid)
    if rep:
        await db.delete(rep)
        await db.commit()
    return {"ok": True}
