"""字节跳动招聘 API 爬虫（jobs.bytedance.com 公开搜索接口，带 CSRF token）。"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import unquote

import httpx

from app.services.jobs.base import DEFAULT_KEYWORDS, JobItem, JobSource


logger = logging.getLogger(__name__)

CSRF_URL = "https://jobs.bytedance.com/api/v1/csrf/token"
LIST_URL = "https://jobs.bytedance.com/api/v1/search/job/posts"
JOB_PAGE_TPL = "https://jobs.bytedance.com/experienced/position/{post_id}/detail"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
REFERER = "https://jobs.bytedance.com/experienced/position"


class ByteDanceSource(JobSource):
    name = "bytedance"
    label = "字节跳动"

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
                "Referer": REFERER,
                "Origin": "https://jobs.bytedance.com",
            },
            follow_redirects=True,
        ) as client:
            try:
                csrf_token = await self._csrf(client)
            except Exception as e:
                logger.warning("ByteDance CSRF init failed: %s", e)
                return items

            for kw in kws:
                try:
                    posts = await self._query(
                        client, csrf_token, kw, limit=min(10, limit_per_kw)
                    )
                except Exception as e:
                    logger.warning("ByteDance query %s failed: %s", kw, e)
                    continue
                for p in posts[:limit_per_kw]:
                    pid = str(p.get("id") or "")
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    items.append(
                        JobItem(
                            source=self.name,
                            source_post_id=pid,
                            title=p.get("title") or p.get("name") or "",
                            raw_url=JOB_PAGE_TPL.format(post_id=pid),
                            category=self._category(p.get("job_category")),
                            location=self._first_city(
                                p.get("city_info") or p.get("city_list")
                            ),
                            department=self._first(p.get("department")),
                            keyword=kw,
                            responsibility=p.get("description", ""),
                            requirement=p.get("requirement", ""),
                        )
                    )
        return items

    async def _csrf(self, client: httpx.AsyncClient) -> str:
        # 触发服务端下发 atsx-csrf-token cookie 与响应中的 token
        r = await client.post(
            CSRF_URL,
            data={"portal_entrance": "1"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        # 优先从 cookie 取，URL 解码即可作为 x-csrf-token 头部值
        cookie_token = client.cookies.get("atsx-csrf-token")
        if cookie_token:
            return unquote(cookie_token)
        body_token = (r.json().get("data") or {}).get("token")
        if not body_token:
            raise RuntimeError("CSRF token missing")
        return body_token

    async def _query(
        self,
        client: httpx.AsyncClient,
        csrf_token: str,
        keyword: str,
        *,
        limit: int = 10,
    ) -> list[dict]:
        body = {
            "keyword": keyword,
            "limit": limit,
            "offset": 0,
            "job_category_id_list": [],
            "tag_id_list": [],
            "location_code_list": [],
            "subject_id_list": [],
            "head_id_list": [],
            "recruitment_id_list": [],
            "sequence_id_list": [],
            # portal_type=2 为社招岗位列表（V3 接口）；6 已被弃用导致 405
            "portal_type": 2,
            "portal_entrance": 1,
        }
        r = await client.post(
            LIST_URL,
            json=body,
            headers={
                "Content-Type": "application/json",
                "x-csrf-token": csrf_token,
            },
        )
        r.raise_for_status()
        data = r.json().get("data") or {}
        posts = data.get("job_post_list") or data.get("posts") or []
        return posts

    @staticmethod
    def _first(v: Any) -> str:
        if isinstance(v, dict):
            return str(v.get("name") or v.get("title") or "")
        if isinstance(v, list) and v:
            return ByteDanceSource._first(v[0])
        return str(v or "")

    @staticmethod
    def _category(v: Any) -> str:
        # job_category 形如 {"name": "后端", "parent": {"name": "研发"}}
        if isinstance(v, dict):
            parent = v.get("parent")
            child = v.get("name") or ""
            if isinstance(parent, dict) and parent.get("name"):
                return f"{parent['name']}-{child}".strip("-")
            return str(child)
        if isinstance(v, list) and v:
            return ByteDanceSource._category(v[0])
        return str(v or "")

    @staticmethod
    def _first_city(v: Any) -> str:
        if isinstance(v, dict):
            return str(v.get("name") or "")
        if isinstance(v, list) and v:
            return ByteDanceSource._first_city(v[0])
        return ""
