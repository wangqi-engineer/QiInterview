"""PDF 简历解析：纯文本抽取 + LLM 结构化。"""
from __future__ import annotations

import io
from typing import Any

from pypdf import PdfReader

from app.core.credentials import LLMCreds
from app.services.llm import chat_complete, render_prompt, safe_parse_json


def extract_text_from_pdf(content: bytes) -> str:
    """提取 PDF 全部文本。"""
    reader = PdfReader(io.BytesIO(content))
    parts: list[str] = []
    for page in reader.pages:
        try:
            txt = page.extract_text() or ""
        except Exception:
            txt = ""
        if txt.strip():
            parts.append(txt.strip())
    return "\n".join(parts)


async def structure_resume(creds: LLMCreds, raw_text: str) -> dict[str, Any]:
    """用 LLM 把 PDF 文本结构化。"""
    if not raw_text.strip():
        return {
            "name": "",
            "education": "",
            "years_of_exp": 0,
            "skills": [],
            "projects": [],
            "summary": "",
        }
    prompt = render_prompt("resume_extract.j2", raw_text=raw_text)
    raw = await chat_complete(
        creds,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=900,
        response_format_json=True,
    )
    return safe_parse_json(raw)


def summarize_resume(structured: dict[str, Any], raw_text: str) -> str:
    """生成给 LLM 上下文用的简洁摘要文本。"""
    if not structured:
        return raw_text[:1500]
    skills = structured.get("skills") or []
    projects = structured.get("projects") or []
    parts: list[str] = []
    if structured.get("name"):
        parts.append(f"姓名：{structured['name']}")
    if structured.get("education"):
        parts.append(f"学历：{structured['education']}")
    if structured.get("years_of_exp"):
        parts.append(f"经验：{structured['years_of_exp']} 年")
    if skills:
        parts.append("技能：" + "、".join(map(str, skills[:15])))
    if projects:
        parts.append("项目经验：")
        for p in projects[:5]:
            parts.append(f"- {p.get('name', '')}: {p.get('summary', '')}")
    if structured.get("summary"):
        parts.append("综述：" + structured["summary"])
    if not parts:
        return raw_text[:1500]
    return "\n".join(parts)
