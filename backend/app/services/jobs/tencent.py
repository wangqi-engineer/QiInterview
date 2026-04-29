"""腾讯招聘 API 爬虫（careers.tencent.com 公开接口）。"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app.services.jobs.base import DEFAULT_KEYWORDS, JobItem, JobSource


logger = logging.getLogger(__name__)

LIST_URL = "https://careers.tencent.com/tencentcareer/api/post/Query"
DETAIL_URL = "https://careers.tencent.com/tencentcareer/api/post/ByPostId"
JOB_PAGE_TPL = "https://careers.tencent.com/jobdesc.html?postId={post_id}"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


class TencentSource(JobSource):
    name = "tencent"
    label = "腾讯"

    async def fetch(
        self, *, keywords: list[str] | None = None, limit_per_kw: int = 10
    ) -> list[JobItem]:
        kws = keywords or DEFAULT_KEYWORDS
        items: list[JobItem] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(
            timeout=20, headers={"User-Agent": UA, "Accept": "application/json"}
        ) as client:
            for kw in kws:
                try:
                    posts = await self._query(client, kw, page_size=min(10, limit_per_kw))
                except Exception as e:
                    logger.warning("Tencent query %s failed: %s", kw, e)
                    continue
                # 详情页可以获得职责与要求
                for p in posts[:limit_per_kw]:
                    pid = str(p.get("PostId") or "")
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    detail = await self._detail(client, pid)
                    items.append(
                        JobItem(
                            source=self.name,
                            source_post_id=pid,
                            title=p.get("RecruitPostName", ""),
                            raw_url=JOB_PAGE_TPL.format(post_id=pid),
                            category=p.get("CategoryName", ""),
                            location=p.get("LocationName", ""),
                            department=p.get("BGName", ""),
                            keyword=kw,
                            responsibility=detail.get("Responsibility", ""),
                            requirement=detail.get("Requirement", ""),
                        )
                    )
                    await asyncio.sleep(0.3)
        return items

    async def _query(
        self, client: httpx.AsyncClient, keyword: str, *, page_size: int = 10
    ) -> list[dict]:
        params = {
            "timestamp": int(time.time() * 1000),
            "keyword": keyword,
            "pageIndex": 1,
            "pageSize": page_size,
            "language": "zh-cn",
            "area": "cn",
        }
        r = await client.get(LIST_URL, params=params)
        r.raise_for_status()
        data = r.json().get("Data") or {}
        return list(data.get("Posts") or [])

    async def _detail(self, client: httpx.AsyncClient, post_id: str) -> dict:
        params = {
            "timestamp": int(time.time() * 1000),
            "postId": post_id,
            "language": "zh-cn",
        }
        try:
            r = await client.get(DETAIL_URL, params=params)
            r.raise_for_status()
            return r.json().get("Data") or {}
        except Exception as e:
            logger.debug("Tencent detail %s failed: %s", post_id, e)
            return {}
