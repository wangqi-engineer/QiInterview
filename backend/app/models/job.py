"""岗位库数据表（缓存大厂招聘信息）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class JobPost(Base):
    __tablename__ = "job_post"
    __table_args__ = (
        UniqueConstraint("source", "source_post_id", name="uq_source_post"),
        Index("ix_job_source_keyword", "source", "keyword"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(20))  # tencent | bytedance | alibaba
    source_post_id: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(100), default="")
    location: Mapped[str] = mapped_column(String(100), default="")
    department: Mapped[str] = mapped_column(String(120), default="")
    keyword: Mapped[str] = mapped_column(String(60), default="")
    responsibility: Mapped[str] = mapped_column(Text, default="")
    requirement: Mapped[str] = mapped_column(Text, default="")
    raw_url: Mapped[str] = mapped_column(String(500))

    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime)

    @property
    def jd(self) -> str:
        parts: list[str] = []
        if self.responsibility:
            parts.append("【岗位职责】\n" + self.responsibility)
        if self.requirement:
            parts.append("【任职要求】\n" + self.requirement)
        return "\n\n".join(parts).strip()
