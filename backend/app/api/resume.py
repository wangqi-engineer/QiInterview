"""简历上传与解析 REST。

s13 用户合同：``/api/resume/upload`` 必须在 **3 秒** 内回包，否则前端按钮的
loading 状态会让用户以为页面卡死。

历史上这个端点会同步阻塞地调用 LLM 的 ``structure_resume``（典型 1–20 s
取决于火山方舟当时排队），即把"按钮转圈时长 = LLM 端到端时长"。把这条
路径变得跟 LLM 无关后，端到端只剩磁盘 I/O + ``pypdf.extract_text``，
通常 <500 ms。

下游影响：``InterviewSession.resume_text`` 现在拿到的是 PDF 抽取出的原文
前 1500 字（即 ``summary`` 与之前 LLM 失败时的兜底完全一致）。面试引擎
本身就是 LLM 驱动，能够直接消费半结构化的简历原文，质量基本无损。
"""
from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.services.resume_parser import extract_text_from_pdf


router = APIRouter(prefix="/resume", tags=["resume"])

# s14 / s15：单文件大小硬上限。与前端 ``RESUME_MAX_MB`` 同步。
# 超过即直接 4xx 拒绝，避免恶意客户端把后端打到 OOM。
MAX_RESUME_BYTES = 5 * 1024 * 1024  # 5 MB

# s16：可识别的 PDF 内容头（``%PDF-``）。用 magic-byte 校验，
# 不仅看后缀；防止把 PE / 脚本 / 反序列化载荷伪装成 .pdf 上来。
_PDF_MAGIC = b"%PDF-"

# s13 / s19 解耦：之前 ``summary`` / ``raw_text`` 都被硬截到 1500/6000 字符，
# 直接被前端 textarea 当作"用户可见的简历内容"展示。这违反了用户合同 s19：
# 『上传 profile.pdf 后下面的文本框中应能看到 profile.md 中所有内容』 ——
# 即 PDF 文本展示侧不能再被截断。
#
# 解决方案：
#   - 这两个常量只控制"前端 textarea 展示侧"的上限，放大到 50000 字符，
#     足以装下任何合理简历的全文（profile.md ≈ 3300 字符）。
#   - 真正的 LLM 上下文截断由 ``backend/app/api/interview.py::create_interview``
#     用 ``RESUME_LLM_CONTEXT_CHARS`` 控制（保持 ≤ 6000，避免撑爆 LLM 输入）。
_SUMMARY_CHARS = 50000
_RAW_TEXT_CHARS = 50000


@router.post("/upload")
async def upload_resume(file: UploadFile = File(...)) -> dict:
    if not file.filename:
        raise HTTPException(400, "缺少文件名")
    name_lower = file.filename.lower()
    raw = await file.read()
    if len(raw) > MAX_RESUME_BYTES:
        raise HTTPException(
            413,
            f"文件大小 {len(raw)/1024/1024:.1f} MB 超过 "
            f"{MAX_RESUME_BYTES/1024/1024:.0f} MB 上限",
        )
    text = ""
    if name_lower.endswith(".pdf"):
        if not raw.startswith(_PDF_MAGIC):
            raise HTTPException(
                400, "文件后缀为 .pdf 但内容不是合法 PDF（缺失 %PDF- 头）"
            )
        try:
            text = extract_text_from_pdf(raw)
        except Exception as e:
            raise HTTPException(400, f"PDF 解析失败: {e}")
    elif name_lower.endswith((".txt", ".md")):
        text = raw.decode("utf-8", errors="ignore")
    else:
        raise HTTPException(400, "仅支持 PDF / TXT / MD")

    if not text.strip():
        raise HTTPException(400, "未从文件中提取到任何文本")

    return {
        "filename": file.filename,
        "raw_text": text[:_RAW_TEXT_CHARS],
        "summary": text[:_SUMMARY_CHARS],
        "structured": {},
    }
