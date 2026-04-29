"""每用户隔离的 API key 缓存。

P3 / a3 用户合同：
  - ``alice`` 写过 LLM key，``bob`` 登录后 ``GET /api/credentials.llm_key`` 必须为空；
  - alice 重新登录则能取回。

设计说明：
  - 之前 LLM/语音 key 只在前端 zustand 内存里飘，刷新页面就丢、跨设备不能同步、
    跨用户没隔离。引入 ``UserCredential`` 表后：
      * 写：``PUT /api/credentials`` → upsert 当前用户的行；
      * 读：``GET /api/credentials`` → 返回当前用户的行（不存在则全空）。
  - 每个用户的行通过 ``current_user`` 依赖隔离，无需在 SQL 上手写 user_id 校验。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_dep import current_user
from app.db.session import get_db
from app.models.user import User, UserCredential


router = APIRouter(prefix="/credentials", tags=["credentials"])


class CredentialsBody(BaseModel):
    """所有字段都是可选 partial-update：
    - 缺省 = 不动；
    - ``""`` = 显式置空（用于"清除已保存的 key"）。
    """

    llm_provider: str | None = None
    llm_key: str | None = None
    llm_model: str | None = None
    llm_model_fast: str | None = None
    llm_model_deep: str | None = None
    # v0.4：火山引擎语音单 X-Api-Key（TTS + STT 共用）
    volc_voice_key: str | None = None

    # ── 历史字段（向后兼容，业务代码不再读取） ──
    dashscope_key: str | None = None
    voice_app_id: str | None = None
    voice_token: str | None = None
    voice_tts_app_id: str | None = None
    voice_tts_token: str | None = None
    voice_stt_app_id: str | None = None
    voice_stt_token: str | None = None
    voice_tts_rid: str | None = None
    voice_asr_rid: str | None = None


def _to_dict(row: UserCredential | None) -> dict:
    if row is None:
        return {
            "llm_provider": "doubao",
            "llm_key": "",
            "llm_model": "",
            "llm_model_fast": "",
            "llm_model_deep": "",
            "volc_voice_key": "",
            "dashscope_key": "",
            "voice_app_id": "",
            "voice_token": "",
            "voice_tts_app_id": "",
            "voice_tts_token": "",
            "voice_stt_app_id": "",
            "voice_stt_token": "",
            "voice_tts_rid": "",
            "voice_asr_rid": "",
        }
    return {
        "llm_provider": row.llm_provider or "doubao",
        "llm_key": row.llm_key or "",
        "llm_model": row.llm_model or "",
        "llm_model_fast": row.llm_model_fast or "",
        "llm_model_deep": row.llm_model_deep or "",
        "volc_voice_key": getattr(row, "volc_voice_key", "") or "",
        "dashscope_key": row.dashscope_key or "",
        "voice_app_id": row.voice_app_id or "",
        "voice_token": row.voice_token or "",
        "voice_tts_app_id": row.voice_tts_app_id or "",
        "voice_tts_token": row.voice_tts_token or "",
        "voice_stt_app_id": row.voice_stt_app_id or "",
        "voice_stt_token": row.voice_stt_token or "",
        "voice_tts_rid": row.voice_tts_rid or "",
        "voice_asr_rid": row.voice_asr_rid or "",
    }


@router.get("")
async def get_credentials(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(UserCredential, user.id)
    return _to_dict(row)


@router.put("")
async def upsert_credentials(
    body: CredentialsBody,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await db.get(UserCredential, user.id)
    if row is None:
        row = UserCredential(user_id=user.id)
        db.add(row)
    fields = body.model_dump(exclude_unset=True)
    for k, v in fields.items():
        setattr(row, k, v)
    await db.commit()
    await db.refresh(row)
    return _to_dict(row)
