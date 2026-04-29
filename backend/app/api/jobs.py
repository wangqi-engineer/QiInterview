"""岗位库 REST。"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.job import JobListResponse, JobOut
from app.services import llm_mock
from app.services.jobs.cache import has_fresh_data, list_jobs
from app.services.jobs.refresher import refresh_all_sources


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/jobs", tags=["jobs"])


def _auto_refresh_disabled() -> bool:
    """测试 / mock 环境下关闭外网抓取，避免阻塞。"""
    return os.environ.get("QI_DISABLE_AUTO_REFRESH", "").strip() in {"1", "true", "True", "yes"} or llm_mock.is_mock_enabled()


@router.get("", response_model=JobListResponse)
async def get_jobs(
    background: BackgroundTasks,
    source: str | None = Query(default=None),
    q: str | None = Query(default=None, description="关键词检索"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    refresh: bool = Query(default=False, description="强制后台刷新"),
    db: AsyncSession = Depends(get_db),
) -> JobListResponse:
    cached = await has_fresh_data(db)
    if (refresh or not cached) and not _auto_refresh_disabled():
        background.add_task(refresh_all_sources)
    items, total = await list_jobs(db, source=source, q=q, page=page, page_size=page_size)
    return JobListResponse(
        items=[JobOut.model_validate(i) for i in items],
        total=total,
        page=page,
        page_size=page_size,
        cached=cached,
    )


@router.post("/refresh")
async def force_refresh() -> dict:
    """同步触发全量刷新（调试用）。"""
    return await refresh_all_sources()
