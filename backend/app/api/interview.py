"""面试 REST：创建 / 列表 / 详情 / 删除。

创建支持两种模式：
- 默认（同步）：等 ``compute_initial_score`` 算完再返回，``impression_breakdown.status="ready"``。
- ``?async_score=1``：立刻插库返回 sid + ``status="pending"``，后台跑印象分；
  前端轮询 ``GET /interviews/{sid}`` 直到 ``status="ready"``。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import delete as sa_delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.core.auth_dep import current_user
from app.core.credentials import LLMCreds, llm_credentials
from app.core.voice_router import pick_speaker
from app.db.session import AsyncSessionLocal, get_db
from app.models.interview import InterviewSession, Report, Turn
from app.models.job import JobPost
from app.models.user import User
from app.schemas.interview import (
    InterviewCreate,
    InterviewDetail,
    InterviewListPage,
    InterviewOut,
    TurnOut,
)
from app.services.scoring import _normalize_breakdown, compute_initial_score


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/interviews", tags=["interviews"])

# s19 解耦：``/api/resume/upload`` 现在返回 PDF 全文（最多 50000 字符）
# 用以前端 textarea 完整展示。但 ``InterviewSession.resume_text`` 会被 LLM
# 当上下文喂下去 —— 需要一个独立的、保守的截断阈值，防止简历过长撑爆 LLM
# 输入预算（Doubao / DeepSeek 通常 8 K–128 K，但费用与延迟随长度线性增长）。
RESUME_LLM_CONTEXT_CHARS = 6000


def _truncate_resume_for_llm(text: str | None) -> str:
    return (text or "")[:RESUME_LLM_CONTEXT_CHARS]


def _ready_breakdown(reason: str, breakdown: dict) -> dict:
    return {"status": "ready", "reason": reason, "dimensions": breakdown}


def _pending_breakdown() -> dict:
    return {"status": "pending", "reason": "", "dimensions": _normalize_breakdown(None)}


async def _bg_compute_initial_score(
    sid: str,
    creds: LLMCreds,
    *,
    resume_text: str,
    job_title: str,
    job_jd: str,
) -> None:
    """后台计算印象分并 update DB。失败不抛，仅落 ``status="error"``。"""
    try:
        score, reason, breakdown = await compute_initial_score(
            creds,
            resume_text=resume_text,
            job_title=job_title,
            job_jd=job_jd,
        )
        result = _ready_breakdown(reason, breakdown)
    except Exception as e:
        logger.warning("BG compute_initial_score 失败 sid=%s: %s", sid, e)
        s = get_settings()
        score = (s.initial_score_min + s.initial_score_max) // 2
        result = {
            "status": "error",
            "reason": f"印象分计算失败：{e}",
            "dimensions": _normalize_breakdown(None),
        }

    async with AsyncSessionLocal() as db:
        row = await db.get(InterviewSession, sid)
        if row is None:
            return
        old_initial = row.initial_score
        row.initial_score = score
        # 如果 final_score 还停留在 fallback 起始值，跟着更新
        if row.final_score == old_initial:
            row.final_score = score
        row.impression_breakdown = result
        await db.commit()


@router.post("", response_model=InterviewOut)
async def create_interview(
    payload: InterviewCreate,
    background_tasks: BackgroundTasks,
    creds: LLMCreds = Depends(llm_credentials),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
    async_score: bool = Query(default=False, alias="async_score"),
) -> InterviewOut:
    job: JobPost | None = None
    if payload.job_id:
        job = await db.get(JobPost, payload.job_id)
        if not job:
            raise HTTPException(404, "岗位不存在")

    job_title = payload.job_title or (job.title if job else "")
    job_jd = payload.job_jd or (job.jd if job else "")
    job_url = payload.job_url or (job.raw_url if job else "")

    if not job_title:
        raise HTTPException(400, "必须提供 job_id 或 job_title")

    speaker = pick_speaker(payload.interview_type)
    settings = get_settings()

    resume_for_llm = _truncate_resume_for_llm(payload.resume_text)

    if async_score:
        # 异步路径：立刻插库返回 sid，BackgroundTasks 跑印象分
        fallback = (settings.initial_score_min + settings.initial_score_max) // 2
        session = InterviewSession(
            id=uuid.uuid4().hex,
            interview_type=payload.interview_type,
            eval_mode=payload.eval_mode,
            llm_provider=payload.llm_provider,
            llm_model=payload.llm_model,
            voice_speaker=speaker,
            job_id=job.id if job else None,
            job_title=job_title,
            job_jd=job_jd,
            job_url=job_url,
            resume_text=resume_for_llm,
            resume_filename=payload.resume_filename or "",
            initial_score=fallback,
            final_score=fallback,
            impression_breakdown=_pending_breakdown(),
            user_id=user.id,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        background_tasks.add_task(
            _bg_compute_initial_score,
            session.id,
            creds,
            resume_text=resume_for_llm,
            job_title=job_title,
            job_jd=job_jd,
        )
        return InterviewOut.model_validate(session)

    # 默认同步路径（保持向后兼容）：等印象分算完再返回
    initial_score, reason, breakdown = await compute_initial_score(
        creds,
        resume_text=resume_for_llm,
        job_title=job_title,
        job_jd=job_jd,
    )
    session = InterviewSession(
        id=uuid.uuid4().hex,
        interview_type=payload.interview_type,
        eval_mode=payload.eval_mode,
        llm_provider=payload.llm_provider,
        llm_model=payload.llm_model,
        voice_speaker=speaker,
        job_id=job.id if job else None,
        job_title=job_title,
        job_jd=job_jd,
        job_url=job_url,
        resume_text=resume_for_llm,
        resume_filename=payload.resume_filename or "",
        initial_score=initial_score,
        final_score=initial_score,
        impression_breakdown=_ready_breakdown(reason, breakdown),
        user_id=user.id,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return InterviewOut.model_validate(session)


@router.get("")
async def list_interviews(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=100),
    limit: int | None = Query(default=None, ge=1, le=200),
):
    """复盘列表查询。

    两种调用方式（背靠背向后兼容）：
    - **分页**（``HistoryPage`` 用）：``?page=1&page_size=10`` → 返回
      ``InterviewListPage{items,total,page,page_size}``。记录上量后避免一次性
      拉 50/200 条把首屏 LCP 拖崩。
    - **平铺**（老调试脚本 / ``test_A7`` 老契约）：``?limit=N`` → 返回
      ``list[InterviewOut]``。仅在没传 page/page_size 时走这条分支。

    缺省（无任何参数）走分页：``page=1, page_size=10``。
    """
    paginated = page is not None or page_size is not None
    legacy_limit = limit is not None and not paginated

    if legacy_limit:
        stmt = (
            select(InterviewSession)
            .where(InterviewSession.user_id == user.id)
            .order_by(desc(InterviewSession.created_at))
            .limit(int(limit))
        )
        rows = (await db.execute(stmt)).scalars().all()
        return [InterviewOut.model_validate(r).model_dump(mode="json") for r in rows]

    eff_page = page or 1
    eff_size = page_size or 10
    offset = (eff_page - 1) * eff_size

    total_stmt = (
        select(func.count(InterviewSession.id))
        .where(InterviewSession.user_id == user.id)
    )
    total = (await db.execute(total_stmt)).scalar_one()

    stmt = (
        select(InterviewSession)
        .where(InterviewSession.user_id == user.id)
        .order_by(desc(InterviewSession.created_at))
        .offset(offset)
        .limit(eff_size)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return InterviewListPage(
        items=[InterviewOut.model_validate(r) for r in rows],
        total=int(total or 0),
        page=eff_page,
        page_size=eff_size,
    )


@router.get("/{sid}", response_model=InterviewDetail)
async def get_interview(
    sid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> InterviewDetail:
    stmt = (
        select(InterviewSession)
        .where(InterviewSession.id == sid, InterviewSession.user_id == user.id)
        .options(selectinload(InterviewSession.turns))
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "面试不存在")
    detail = InterviewDetail.model_validate(row)
    detail.turns = [TurnOut.model_validate(t) for t in row.turns]
    return detail


@router.delete("")
async def delete_all_interviews(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """h6 一键删除：清空 **当前登录用户的** 所有面试会话（含 Turn / Report 关联）。

    P3 / a2 用户合同：用户 A 不能误删用户 B 的复盘。SQL 层把删除语句限定
    ``InterviewSession.user_id == current_user.id``；Turn / Report 通过
    ``session_id IN (...)`` 子查询定位本用户的所有会话再级联删除（SQLite 默认
    没有 ``PRAGMA foreign_keys=ON``，所以 ORM 上的 cascade 不会自动触发）。
    """
    own_sids_subq = select(InterviewSession.id).where(
        InterviewSession.user_id == user.id
    )
    pre_total = (
        await db.execute(
            select(func.count(InterviewSession.id)).where(
                InterviewSession.user_id == user.id
            )
        )
    ).scalar_one() or 0
    if pre_total:
        await db.execute(
            sa_delete(Turn).where(Turn.session_id.in_(own_sids_subq))
        )
        await db.execute(
            sa_delete(Report).where(Report.session_id.in_(own_sids_subq))
        )
        await db.execute(
            sa_delete(InterviewSession).where(
                InterviewSession.user_id == user.id
            )
        )
        await db.commit()
    return {"ok": True, "deleted": int(pre_total)}


@router.delete("/{sid}")
async def delete_interview(
    sid: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    row = await db.get(InterviewSession, sid)
    if not row or row.user_id != user.id:
        raise HTTPException(404, "面试不存在")
    await db.delete(row)
    await db.commit()
    return {"ok": True}


@router.post("/{sid}/end")
async def end_interview(
    sid: str,
    reason: str = "user",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    row = await db.get(InterviewSession, sid)
    if not row or row.user_id != user.id:
        raise HTTPException(404, "面试不存在")
    if row.ended_at is None:
        row.ended_at = datetime.utcnow()
        row.end_reason = reason
        await db.commit()
    return {"ok": True}
