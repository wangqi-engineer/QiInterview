"""面试相关数据表。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class InterviewSession(Base):
    __tablename__ = "interview_session"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    interview_type: Mapped[str] = mapped_column(String(20))  # tech1 | tech2 | comprehensive | hr
    eval_mode: Mapped[str] = mapped_column(String(20))  # realtime | summary
    llm_provider: Mapped[str] = mapped_column(String(20))
    llm_model: Mapped[str] = mapped_column(String(100))
    voice_speaker: Mapped[str] = mapped_column(String(80))

    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("job_post.id"), nullable=True)

    # P3 / lite-auth：归属用户。``nullable=True`` 是为了让历史数据（auth 上线
    # 前的会话）仍可建表迁移；新创建的会话会被强制带上当前登录用户的 id。
    # 列表 / 详情 / 删除接口在用户登录时按 ``user_id == current_user.id`` 过滤；
    # 历史 NULL 行不属于任何具体用户，会被新接口隐藏（"legacy" 不可见）。
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )

    job_title: Mapped[str] = mapped_column(String(200), default="")
    job_jd: Mapped[str] = mapped_column(Text, default="")
    job_url: Mapped[str] = mapped_column(String(500), default="")

    resume_text: Mapped[str] = mapped_column(Text, default="")
    resume_filename: Mapped[str] = mapped_column(String(200), default="")

    initial_score: Mapped[int] = mapped_column(Integer, default=70)
    final_score: Mapped[int] = mapped_column(Integer, default=70)
    end_reason: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    impression_breakdown: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    turns: Mapped[list["Turn"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="Turn.idx"
    )
    report: Mapped[Optional["Report"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", uselist=False
    )


class Turn(Base):
    __tablename__ = "turn"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("interview_session.id", ondelete="CASCADE"))
    idx: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(20))  # interviewer | candidate
    text: Mapped[str] = mapped_column(Text)
    strategy: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # breadth | depth
    expected_topic: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    score_delta: Mapped[int] = mapped_column(Integer, default=0)
    score_after: Mapped[int] = mapped_column(Integer, default=0)
    evaluator_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    session: Mapped[InterviewSession] = relationship(back_populates="turns")


class Report(Base):
    __tablename__ = "report"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("interview_session.id", ondelete="CASCADE"), primary_key=True
    )
    summary: Mapped[str] = mapped_column(Text, default="")
    strengths_md: Mapped[str] = mapped_column(Text, default="")
    weaknesses_md: Mapped[str] = mapped_column(Text, default="")
    advice_md: Mapped[str] = mapped_column(Text, default="")
    score_explanation_md: Mapped[str] = mapped_column(Text, default="")
    trend_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    session: Mapped[InterviewSession] = relationship(back_populates="report")
