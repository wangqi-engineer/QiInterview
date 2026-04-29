"""岗位爬虫抽象。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class JobItem:
    source: str
    source_post_id: str
    title: str
    raw_url: str
    category: str = ""
    location: str = ""
    department: str = ""
    keyword: str = ""
    responsibility: str = ""
    requirement: str = ""


# 默认搜索关键词（覆盖 AI / 大模型方向）
DEFAULT_KEYWORDS: list[str] = [
    "大模型",
    "AIGC",
    "算法",
    "AI",
    "机器学习",
]


class JobSource(ABC):
    """岗位数据源抽象。子类实现 `fetch()` 返回结构化岗位列表。"""

    name: str = "base"
    label: str = "base"

    @abstractmethod
    async def fetch(self, *, keywords: list[str] | None = None, limit_per_kw: int = 10) -> list[JobItem]:
        """拉取岗位（带原始链接）。"""
        ...
