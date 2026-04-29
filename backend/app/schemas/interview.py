"""面试相关 Pydantic 模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


InterviewType = Literal["tech1", "tech2", "comprehensive", "hr"]
EvalMode = Literal["realtime", "summary"]
LLMProvider = Literal["doubao", "deepseek", "qwen", "glm"]


class InterviewCreate(BaseModel):
    interview_type: InterviewType
    eval_mode: EvalMode
    llm_provider: LLMProvider = "doubao"
    llm_model: str = "doubao-seed-1-6-251015"
    job_id: Optional[int] = None
    job_title: Optional[str] = None
    job_jd: Optional[str] = None
    job_url: Optional[str] = None
    resume_text: Optional[str] = ""
    resume_filename: Optional[str] = ""


class TurnOut(BaseModel):
    id: int
    idx: int
    role: str
    text: str
    strategy: Optional[str] = None
    expected_topic: Optional[str] = None
    score_delta: int
    score_after: int
    evaluator_json: Optional[dict[str, Any]] = None
    started_at: datetime
    ended_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class InterviewOut(BaseModel):
    id: str
    interview_type: str
    eval_mode: str
    llm_provider: str
    llm_model: str
    voice_speaker: str
    job_id: Optional[int] = None
    job_title: str = ""
    job_url: str = ""
    job_jd: str = ""
    resume_filename: str = ""
    initial_score: int
    final_score: int
    end_reason: Optional[str] = None
    created_at: datetime
    ended_at: Optional[datetime] = None
    impression_breakdown: Optional[dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)


class InterviewDetail(InterviewOut):
    turns: list[TurnOut] = Field(default_factory=list)


class InterviewListPage(BaseModel):
    """分页响应：``HistoryPage`` 列表数据量上来后，单次拉 50/200 条会让首屏 LCP
    崩盘 + DB SELECT 慢 + 前端渲染抖动。改成显式分页。
    """

    items: list[InterviewOut]
    total: int
    page: int
    page_size: int


class TrendPoint(BaseModel):
    idx: int
    score: int
    delta: int


class ReportOut(BaseModel):
    session_id: str
    summary: str
    strengths_md: str
    weaknesses_md: str
    advice_md: str
    score_explanation_md: str = ""
    trend: list[TrendPoint]
    turns: list[TurnOut]
    initial_score: int = 0
    final_score: int = 0
    impression_breakdown: Optional[dict[str, Any]] = None
    created_at: datetime
