"""定时刷新岗位库（APScheduler + 启动时按需刷新）。"""
from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.db.session import AsyncSessionLocal
from app.services.jobs import all_sources
from app.services.jobs.cache import has_fresh_data, upsert_jobs


logger = logging.getLogger(__name__)


async def refresh_all_sources() -> dict[str, int]:
    """从所有来源拉取并写入缓存。"""
    out: dict[str, int] = {}
    sources = all_sources()
    sem = asyncio.Semaphore(3)

    async def _one(src):
        async with sem:
            try:
                items = await src.fetch(limit_per_kw=8)
            except Exception as e:
                logger.warning("source %s fetch failed: %s", src.name, e)
                return src.name, 0, []
            return src.name, len(items), items

    results = await asyncio.gather(*(_one(s) for s in sources))

    async with AsyncSessionLocal() as db:
        for name, n_fetched, items in results:
            try:
                n_written = await upsert_jobs(db, items)
                out[name] = n_written
                logger.info("refreshed %s: fetched=%d written=%d", name, n_fetched, n_written)
            except Exception as e:
                logger.warning("source %s upsert failed: %s", name, e)
                out[name] = 0
    return out


_scheduler: AsyncIOScheduler | None = None


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    s = get_settings()
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        refresh_all_sources,
        IntervalTrigger(hours=s.jobs_refresh_interval_hours),
        id="jobs_refresh",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Jobs refresh scheduler started (every %dh)", s.jobs_refresh_interval_hours)


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


async def warmup_if_empty() -> None:
    async with AsyncSessionLocal() as db:
        if not await has_fresh_data(db):
            logger.info("Job cache empty / stale, warming up...")
            try:
                await refresh_all_sources()
            except Exception as e:
                logger.warning("warmup failed: %s", e)
