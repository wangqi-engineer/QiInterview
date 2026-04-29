"""岗位库 Pydantic 模型。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class JobOut(BaseModel):
    id: int
    source: str
    source_post_id: str
    title: str
    category: str = ""
    location: str = ""
    department: str = ""
    keyword: str = ""
    responsibility: str = ""
    requirement: str = ""
    raw_url: str
    fetched_at: datetime
    expires_at: datetime

    model_config = ConfigDict(from_attributes=True)


class JobListResponse(BaseModel):
    items: list[JobOut]
    total: int
    page: int
    page_size: int
    cached: bool
