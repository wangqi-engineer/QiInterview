"""阿里巴巴招聘 API 爬虫。

使用 careers.alibabacloud.com 集团招聘门户，需要先访问列表页拿 XSRF-TOKEN，
再在 POST 请求里把它放到 X-XSRF-TOKEN 头部，并保留 SESSION cookie。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.services.jobs.base import DEFAULT_KEYWORDS, JobItem, JobSource


logger = logging.getLogger(__name__)

WARMUP_URL = "https://careers.alibabacloud.com/off-campus/position-list"
LIST_URL = "https://careers.alibabacloud.com/position/search"
JOB_PAGE_TPL = (
    "https://careers.alibabacloud.com/off-campus/position-detail?positionId={post_id}"
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class AlibabaSource(JobSource):
    name = "alibaba"
    label = "阿里巴巴"

    async def fetch(
        self, *, keywords: list[str] | None = None, limit_per_kw: int = 10
    ) -> list[JobItem]:
        kws = keywords or DEFAULT_KEYWORDS
        items: list[JobItem] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(
            timeout=20,
            headers={
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://careers.alibabacloud.com",
                "Referer": WARMUP_URL,
            },
            follow_redirects=True,
        ) as client:
            try:
                xsrf = await self._warmup(client)
            except Exception as e:
                logger.warning("Alibaba warmup failed: %s", e)
                return items

            for kw in kws:
                try:
                    posts = await self._query(
                        client, xsrf, kw, page_size=min(10, limit_per_kw)
                    )
                except Exception as e:
                    logger.warning("Alibaba query %s failed: %s", kw, e)
                    continue
                for p in posts[:limit_per_kw]:
                    pid = str(p.get("id") or p.get("positionId") or "")
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    location = self._joined(p.get("workLocations"))
                    items.append(
                        JobItem(
                            source=self.name,
                            source_post_id=pid,
                            title=p.get("name") or p.get("title") or "",
                            raw_url=JOB_PAGE_TPL.format(post_id=pid),
                            category=self._joined(p.get("categories")),
                            location=location or self._joined(p.get("interviewLocations")),
                            department=p.get("departmentName")
                            or p.get("brandName") or "",
                            keyword=kw,
                            responsibility=p.get("description", "")
                            or p.get("responsibilities", ""),
                            requirement=p.get("requirement", "")
                            or p.get("requirements", ""),
                        )
                    )
        return items

    async def _warmup(self, client: httpx.AsyncClient) -> str:
        r = await client.get(WARMUP_URL)
        r.raise_for_status()
        token = client.cookies.get("XSRF-TOKEN")
        if not token:
            raise RuntimeError("XSRF-TOKEN cookie missing")
        return token

    async def _query(
        self,
        client: httpx.AsyncClient,
        xsrf: str,
        keyword: str,
        *,
        page_size: int = 10,
    ) -> list[dict]:
        body = {
            "keyword": keyword,
            "pageIndex": 1,
            "pageSize": page_size,
            "channel": "group_official_site",
            "language": "zh",
        }
        r = await client.post(
            LIST_URL,
            json=body,
            headers={
                "Content-Type": "application/json",
                "X-XSRF-TOKEN": xsrf,
            },
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict) or not data.get("success"):
            return []
        content = data.get("content") or {}
        return content.get("datas") or content.get("items") or []

    @staticmethod
    def _joined(v: Any) -> str:
        if not v:
            return ""
        if isinstance(v, list):
            parts: list[str] = []
            for item in v:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("displayName") or ""
                    if name:
                        parts.append(str(name))
                elif item:
                    parts.append(str(item))
            return ", ".join(parts)
        if isinstance(v, dict):
            return str(v.get("name") or v.get("displayName") or "")
        return str(v)
