"""岗位库缓存：upsert + TTL 查询。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.job import JobPost
from app.services.jobs.base import JobItem


logger = logging.getLogger(__name__)


async def upsert_jobs(db: AsyncSession, items: Iterable[JobItem]) -> int:
    s = get_settings()
    expires = datetime.utcnow() + timedelta(hours=s.jobs_refresh_interval_hours)
    n = 0
    for it in items:
        stmt = sqlite_insert(JobPost).values(
            source=it.source,
            source_post_id=it.source_post_id,
            title=it.title,
            category=it.category,
            location=it.location,
            department=it.department,
            keyword=it.keyword,
            responsibility=it.responsibility,
            requirement=it.requirement,
            raw_url=it.raw_url,
            fetched_at=datetime.utcnow(),
            expires_at=expires,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["source", "source_post_id"],
            set_={
                "title": stmt.excluded.title,
                "category": stmt.excluded.category,
                "location": stmt.excluded.location,
                "department": stmt.excluded.department,
                "keyword": stmt.excluded.keyword,
                "responsibility": stmt.excluded.responsibility,
                "requirement": stmt.excluded.requirement,
                "raw_url": stmt.excluded.raw_url,
                "fetched_at": stmt.excluded.fetched_at,
                "expires_at": stmt.excluded.expires_at,
            },
        )
        await db.execute(stmt)
        n += 1
    await db.commit()
    return n


async def list_jobs(
    db: AsyncSession,
    *,
    source: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[JobPost], int]:
    stmt = select(JobPost)
    count_stmt = select(func.count(JobPost.id))
    if source:
        stmt = stmt.where(JobPost.source == source)
        count_stmt = count_stmt.where(JobPost.source == source)
    if q:
        like = f"%{q}%"
        cond = or_(
            JobPost.title.ilike(like),
            JobPost.responsibility.ilike(like),
            JobPost.requirement.ilike(like),
            JobPost.keyword.ilike(like),
        )
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    stmt = stmt.order_by(JobPost.fetched_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()
    total = (await db.execute(count_stmt)).scalar_one()
    return list(rows), int(total)


async def has_fresh_data(db: AsyncSession) -> bool:
    """是否存在未过期的缓存。"""
    stmt = select(func.count(JobPost.id)).where(JobPost.expires_at > datetime.utcnow())
    n = (await db.execute(stmt)).scalar_one()
    return int(n) > 0


async def get_job_by_id(db: AsyncSession, job_id: int) -> JobPost | None:
    return await db.get(JobPost, job_id)
