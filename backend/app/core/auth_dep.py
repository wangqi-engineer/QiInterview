"""鉴权依赖：从 cookie ``qi_session`` 取 token → 查 ``Session`` → 返回 ``User``。

P3 / lite-auth 的设计取舍：
  - 用户名唯一注册，无密码 / 无邮箱 / 无 SSO（明确剔除）。
  - cookie 名 ``qi_session``，``HttpOnly`` + ``SameSite=Lax``，30 天有效期。
  - 鉴权失败 → 401，前端路由守卫看到就跳到 ``/login``。
  - 列出 ``current_user_optional`` 仅给 WebSocket / health 这类不强制登录的端点用。
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from fastapi import Cookie, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.user import Session as AuthSession, User


SESSION_COOKIE_NAME = "qi_session"
SESSION_TTL_DAYS = 30


def make_session_token() -> str:
    return secrets.token_hex(32)


def session_expiry() -> datetime:
    return datetime.utcnow() + timedelta(days=SESSION_TTL_DAYS)


async def _resolve_user(
    qi_session: str | None,
    db: AsyncSession,
) -> User | None:
    """查 cookie token → ``User``；过期 / 不存在 → ``None``。"""
    if not qi_session:
        return None
    row = await db.get(AuthSession, qi_session)
    if row is None:
        return None
    if row.expires_at < datetime.utcnow():
        # 过期清掉，避免脏数据堆积
        await db.delete(row)
        await db.commit()
        return None
    user = await db.get(User, row.user_id)
    return user


async def current_user_optional(
    qi_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """非强制依赖：登录则返回 ``User``，未登录返回 ``None``。"""
    return await _resolve_user(qi_session, db)


async def current_user(
    qi_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
) -> User:
    """强制依赖：未登录直接 401。"""
    user = await _resolve_user(qi_session, db)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user


__all__ = [
    "SESSION_COOKIE_NAME",
    "SESSION_TTL_DAYS",
    "make_session_token",
    "session_expiry",
    "current_user",
    "current_user_optional",
]
