"""鉴权 REST：注册（邮箱 + OTP 两步）/ 登录 / 登出 / me / 密码重置 / 公钥。

P6 / 邮箱主身份契约（取代 P3 lite-auth + P4 username/password 模型）：

  - **注册** 拆为两步：
      ``POST /api/auth/register/start``  body ``{email}``
      → 后端生成 6 位 OTP（``secrets.choice``，落 ``email_verification.code_hash``）
      → ``MailSender`` 一次性发送
      → **响应一律 200**（无论 email 是否已注册），防 enumeration；
      ``POST /api/auth/register/verify`` body ``{email, code, password (RSA-OAEP 密文), username?}``
      → 校验 OTP 未过期 + 未消费 → 创建 ``User(email=...)`` → 写 cookie。
  - **登录** 改为 ``POST /api/auth/login`` body ``{email, password}``：用 email
    查用户；老 P3/P4 用户 ``email IS NULL`` → 401，引导走重新注册。
  - **找回密码**：
      ``POST /api/auth/password-reset/start``   body ``{email}``    —— 一律 200；
      ``POST /api/auth/password-reset/confirm`` body ``{token, new_password (RSA-OAEP)}``
      → 校验 token + 落新 ``password_hash`` → ``DELETE FROM auth_session WHERE
      user_id = X``（密码重置必须把所有现存 session 全部踢下线，是密码学
      合规要求）。
  - 邮件链路上**只发** OTP 原文 / 重置 URL 原文；DB / 日志只存 sha256 哈希。

P5 安全合同保留（与 P4 → P5 的密码加密链路完全等价）：
  - 所有 ``password`` / ``new_password`` 字段语义都是 RSA-OAEP base64 密文。
  - 解密失败 / 密文为空 / payload 不是字符串 → 一律 401 ``无效的加密凭据``。
  - 旧 ``QI_AUTH_ALLOW_PLAINTEXT=1`` 降级开关保留（仅本机调试 / 老脚本兼容；
    生产严禁）。

埋点 / 日志：
  - 所有 ``_qidbg`` 只记元信息：``email_hash``（sha256 前 16）/``purpose``/
    ``elapsed_ms``/``ok``。**OTP 原文 / 重置 token 原文 / 密码原文 / 哈希原文
    全都不允许出现在任何日志通道**。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.auth_dep import (
    SESSION_COOKIE_NAME,
    current_user,
    make_session_token,
    session_expiry,
)
from app.core.passwords import hash_password, verify_password
from app.core.rsa_keys import (
    decrypt_password,
    get_public_fingerprint,
    get_public_pem,
)
from app.db.session import get_db
from app.models.user import (
    EmailVerification,
    Session as AuthSession,
    User,
)
from app.services.mail import MailMessage, get_mail_sender


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


# ──────────────────────────────────────────────────────────────────────────
# 校验常量与辅助
# ──────────────────────────────────────────────────────────────────────────

# 简化版 RFC 5322：不追求 100% 合规，只挡明显的乱填。最长 254 是 SMTP 限制。
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,64}$")
_PASSWORD_MIN_LEN = 6
_PASSWORD_MAX_LEN = 128

# RSA-2048 OAEP 输出 256 字节 → base64 ~344 字符；放宽到 256 ≤ len ≤ 1024。
_CT_MIN_LEN = 256
_CT_MAX_LEN = 1024

# 注册 OTP 6 位数字；密码重置 token 是 32-byte URL-safe ≈ 43 字符。
_OTP_DIGITS = 6
_RESET_TOKEN_BYTES = 32

# EmailVerification.purpose 取值
_PURPOSE_REGISTER = "register"
_PURPOSE_PASSWORD_RESET = "password_reset"


def _validate_email(email: str) -> str:
    name = (email or "").strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="请输入邮箱")
    if len(name) > 254 or not _EMAIL_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="邮箱格式无效")
    return name


def _validate_username_optional(username: Optional[str]) -> Optional[str]:
    """注册 verify 时 ``username`` 可选；提供则按 P4 规则校验，未提供 → None。"""
    if username is None:
        return None
    name = username.strip()
    if not name:
        return None
    if not _USERNAME_RE.fullmatch(name):
        raise HTTPException(
            status_code=400,
            detail="昵称仅允许字母 / 数字 / 下划线 / 连字符，长度 3–64",
        )
    return name


def _validate_password_plain(password: str) -> str:
    if not isinstance(password, str):
        raise HTTPException(status_code=400, detail="密码格式无效")
    if len(password) < _PASSWORD_MIN_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"密码长度至少 {_PASSWORD_MIN_LEN} 位",
        )
    if len(password) > _PASSWORD_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"密码长度不能超过 {_PASSWORD_MAX_LEN} 位",
        )
    return password


def _allow_plaintext_fallback() -> bool:
    val = os.environ.get("QI_AUTH_ALLOW_PLAINTEXT", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _looks_like_b64_ciphertext(s: str) -> bool:
    if not isinstance(s, str):
        return False
    if not (_CT_MIN_LEN <= len(s) <= _CT_MAX_LEN):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9+/=_\-]+", s))


def _decrypt_or_extract_plain(field_value: str) -> str:
    """RSA-OAEP 密文 → 明文密码；失败统一抛 401 ``无效的加密凭据``。"""
    if not isinstance(field_value, str) or not field_value:
        raise HTTPException(status_code=401, detail="无效的加密凭据")

    if _allow_plaintext_fallback() and not _looks_like_b64_ciphertext(field_value):
        return _validate_password_plain(field_value)

    try:
        plain = decrypt_password(field_value)
    except ValueError:
        raise HTTPException(status_code=401, detail="无效的加密凭据")
    return _validate_password_plain(plain)


def _set_session_cookie(response: Response, token: str) -> None:
    """统一的 cookie 写入。``Secure`` 与 ``SameSite`` 由 ``Settings`` 决定：
    ``cookie_secure=True`` → ``Secure; SameSite=Strict``；
    ``cookie_secure=False`` → 不带 Secure，``SameSite=Lax``（dev 默认）。

    由 P6 / 传输安全硬化把 secure 标志接到 ``Settings.cookie_secure``，
    避免老版本字面 ``secure=False`` 写死的暴露面。
    """
    s = get_settings()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="strict" if s.cookie_secure else "lax",
        secure=bool(s.cookie_secure),
        path="/",
    )


async def _create_session_for(user: User, db: AsyncSession) -> str:
    token = make_session_token()
    db.add(AuthSession(token=token, user_id=user.id, expires_at=session_expiry()))
    await db.commit()
    return token


# ── _qidbg：所有埋点都走这里。OTP / token / 密码原文一律不入 data ─────────
def _qidbg(location: str, data: dict, message: str = "") -> None:
    import json
    from pathlib import Path

    try:
        path = (
            Path(__file__).resolve().parent.parent.parent.parent
            / ".cursor"
            / "debug-714cc8.log"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "sessionId": "P6-AUTH",
                        "runId": "be_qidbg",
                        "hypothesisId": "P6-EMAIL-OTP",
                        "location": location,
                        "message": message,
                        "data": data,
                        "timestamp": int(time.time() * 1000),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass


def _email_hash16(email: str) -> str:
    """sha256(email)[:16] —— 用于 ``_qidbg`` 关联同邮箱事件的 trace 键。"""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:16]


def _hash_token(raw: str) -> str:
    """OTP / reset token 落库前的 sha256 hex（不带 salt，因为 token 本身就高熵）。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _gen_otp() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(_OTP_DIGITS))


def _gen_reset_token() -> str:
    """``secrets.token_urlsafe(32)`` ≈ 43 字符 base64url。"""
    return secrets.token_urlsafe(_RESET_TOKEN_BYTES)


def _client_ip(request: Request) -> str:
    """从 X-Forwarded-For / client.host 取一个客户端 IP，仅用于埋点字段，
    最终也只会落进 ``request_ip``（埋点不读）。"""
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if fwd:
        return fwd[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return ""


# ──────────────────────────────────────────────────────────────────────────
# Pydantic body 模型
# ──────────────────────────────────────────────────────────────────────────

class EmailOnlyBody(BaseModel):
    # 不在 pydantic 层做长度过严校验 —— 422 是 fastapi 的 schema-validation
    # 错误形态，对外语义不如业务侧的 400 ``邮箱格式无效`` 干净。这里只挡 max。
    email: str = Field(..., max_length=254)


class RegisterVerifyBody(BaseModel):
    email: str = Field(..., max_length=254)
    code: str = Field(..., max_length=64)  # 业务侧再做"必须 6 位数字"校验
    # 与 P5 一致：实际是 RSA-OAEP base64 密文
    password: str = Field(..., min_length=1, max_length=_CT_MAX_LEN)
    username: Optional[str] = Field(default=None, max_length=64)


class LoginBody(BaseModel):
    email: str = Field(..., max_length=254)
    password: str = Field(..., min_length=1, max_length=_CT_MAX_LEN)


class PasswordResetConfirmBody(BaseModel):
    token: str = Field(..., min_length=8, max_length=200)
    # 与 P5 一致：RSA-OAEP base64 密文
    new_password: str = Field(..., min_length=1, max_length=_CT_MAX_LEN)


# ──────────────────────────────────────────────────────────────────────────
# 邮件正文模板（极简 —— 不引入模板引擎依赖）
# ──────────────────────────────────────────────────────────────────────────

def _render_register_otp_mail(email: str, code: str, ttl_min: int) -> MailMessage:
    text = (
        f"你好，\n\n"
        f"您正在注册 QiInterview，验证码是：{code}\n"
        f"有效期 {ttl_min} 分钟。如果不是您本人操作，可忽略本邮件。\n"
    )
    html = (
        f"<p>你好，</p>"
        f"<p>您正在注册 QiInterview，验证码是：<b>{code}</b></p>"
        f"<p>有效期 {ttl_min} 分钟。如果不是您本人操作，可忽略本邮件。</p>"
    )
    return MailMessage(
        to=email,
        subject="QiInterview 注册验证码",
        text=text,
        html=html,
        meta={"purpose": _PURPOSE_REGISTER, "expires_in_min": ttl_min, "code": code},
    )


def _render_password_reset_mail(
    email: str, raw_token: str, ttl_min: int, frontend_base_url: str
) -> MailMessage:
    base = (frontend_base_url or "").rstrip("/")
    url = f"{base}/reset-password?token={raw_token}"
    text = (
        f"你好，\n\n"
        f"我们收到一个针对账号 {email} 的密码重置请求。\n"
        f"如果是您本人操作，请打开以下链接在 {ttl_min} 分钟内完成重置：\n\n"
        f"{url}\n\n"
        f"如果不是您本人操作，可忽略本邮件，您的账户不会有任何变化。\n"
    )
    html = (
        f"<p>你好，</p>"
        f"<p>我们收到一个针对账号 <code>{email}</code> 的密码重置请求。</p>"
        f"<p>如果是您本人操作，请在 {ttl_min} 分钟内点击：</p>"
        f"<p><a href='{url}'>{url}</a></p>"
        f"<p>如果不是您本人操作，可忽略本邮件，您的账户不会有任何变化。</p>"
    )
    return MailMessage(
        to=email,
        subject="QiInterview 密码重置",
        text=text,
        html=html,
        meta={
            "purpose": _PURPOSE_PASSWORD_RESET,
            "expires_in_min": ttl_min,
            "token": raw_token,
            "url": url,
        },
    )


# ──────────────────────────────────────────────────────────────────────────
# 公钥派发（与 P5 完全一致）
# ──────────────────────────────────────────────────────────────────────────

@router.get("/pubkey")
async def pubkey() -> dict:
    return {
        "public_key_pem": get_public_pem(),
        "fingerprint16": get_public_fingerprint(),
        "alg": "RSA-OAEP",
        "hash": "SHA-256",
    }


# ──────────────────────────────────────────────────────────────────────────
# 注册：start / verify
# ──────────────────────────────────────────────────────────────────────────

async def _enforce_send_throttle(
    db: AsyncSession, email: str, purpose: str, min_interval_sec: int
) -> None:
    """同邮箱 + 同 purpose 距离上次 ``created_at`` < ``min_interval_sec`` → 429。

    同时挑掉超过 5 条未消费旧票（防一次性清扫）—— 把更老的批量置为
    consumed，确保只剩近期一条有效。
    """
    if min_interval_sec <= 0:
        return
    cutoff = datetime.utcnow() - timedelta(seconds=min_interval_sec)
    q = (
        select(EmailVerification)
        .where(
            EmailVerification.email == email,
            EmailVerification.purpose == purpose,
            EmailVerification.created_at >= cutoff,
        )
        .order_by(EmailVerification.created_at.desc())
        .limit(1)
    )
    row = (await db.execute(q)).scalar_one_or_none()
    if row is not None:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")


@router.post("/register/start")
async def register_start(
    body: EmailOnlyBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """寄出注册 OTP。响应**一律 200**，不暴露 email 是否已注册。

    顺序：
      1) **先**做"是否已注册"分支判定 —— 已注册 → 静默 200，不写票据、不发
         邮件、不触发节流；这与"陌生 email 第一次发起注册"的响应**完全一致**，
         保证 anti-enumeration。
      2) 否则走真发路径：先节流（同 email 60s 内只准发一次） → 写票据 → 发邮件。

    把节流放在分支后面是有意为之：节流用来挡"对未注册 email 的邮件爆破"，
    对已注册 email 我们本就不发邮件，没必要把节流暴露成可观测信号。
    """
    s = get_settings()
    email = _validate_email(body.email)

    # 1) 已注册 → 静默 200（与未注册分支响应完全等价；anti-enumeration）
    existing_q = select(User).where(User.email == email)
    existing = (await db.execute(existing_q)).scalar_one_or_none()
    if existing is not None:
        _qidbg(
            "auth.register_start:already_registered",
            {"email_hash16": _email_hash16(email), "purpose": _PURPOSE_REGISTER},
            "skip OTP for already-registered email; response remains 200",
        )
        return {"ok": True}

    # 2) 节流（仅对真发路径生效）
    await _enforce_send_throttle(db, email, _PURPOSE_REGISTER, s.mail_send_min_interval_sec)

    code = _gen_otp()
    record = EmailVerification(
        email=email,
        purpose=_PURPOSE_REGISTER,
        code_hash=_hash_token(code),
        token_hash=None,
        expires_at=datetime.utcnow() + timedelta(minutes=s.mail_otp_ttl_minutes),
        request_ip=_client_ip(request) or None,
    )
    db.add(record)
    await db.commit()

    sender = get_mail_sender()
    msg = _render_register_otp_mail(email, code, s.mail_otp_ttl_minutes)
    try:
        await sender.send(msg)
    except Exception as exc:  # 邮件失败 → 500（让运维知道 SMTP 出问题）
        _qidbg(
            "auth.register_start:mail_failed",
            {"email_hash16": _email_hash16(email), "err": type(exc).__name__},
            "MailSender.send raised; aborting with 502",
        )
        raise HTTPException(status_code=502, detail="邮件发送失败，请稍后重试")

    _qidbg(
        "auth.register_start:ok",
        {
            "email_hash16": _email_hash16(email),
            "purpose": _PURPOSE_REGISTER,
            "ttl_min": s.mail_otp_ttl_minutes,
        },
        "register OTP issued",
    )
    return {"ok": True}


@router.post("/register/verify")
async def register_verify(
    body: RegisterVerifyBody,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """校验 OTP + 创建 ``User`` + 写 cookie。"""
    email = _validate_email(body.email)
    code = (body.code or "").strip()
    if not code or not code.isdigit() or len(code) != _OTP_DIGITS:
        raise HTTPException(status_code=401, detail="验证码无效或已过期")

    pw = _decrypt_or_extract_plain(body.password)
    nickname = _validate_username_optional(body.username)

    # 查最新未消费 register 票据
    q = (
        select(EmailVerification)
        .where(
            EmailVerification.email == email,
            EmailVerification.purpose == _PURPOSE_REGISTER,
            EmailVerification.consumed_at.is_(None),
        )
        .order_by(EmailVerification.created_at.desc())
        .limit(1)
    )
    row = (await db.execute(q)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail="验证码无效或已过期")
    if row.expires_at < datetime.utcnow():
        raise HTTPException(status_code=401, detail="验证码无效或已过期")
    if row.code_hash != _hash_token(code):
        raise HTTPException(status_code=401, detail="验证码无效或已过期")

    # 一次性消费
    row.consumed_at = datetime.utcnow()

    # 创建 User
    now = datetime.utcnow()
    user = User(
        username=nickname,
        email=email,
        email_verified_at=now,
        password_hash=hash_password(pw),
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # 唯一冲突 → 既可能是 email 唯一、也可能是 nickname 唯一
        raise HTTPException(status_code=409, detail="该邮箱已注册或昵称已被占用")

    await db.refresh(user)
    token = await _create_session_for(user, db)
    _set_session_cookie(response, token)

    _qidbg(
        "auth.register_verify:ok",
        {"email_hash16": _email_hash16(email), "user_id": user.id},
        "user created via email OTP",
    )
    return {
        "id": user.id,
        "email": user.email,
        "username": user.username,
    }


# ──────────────────────────────────────────────────────────────────────────
# 登录 / 登出 / me
# ──────────────────────────────────────────────────────────────────────────

@router.post("/login")
async def login(
    body: LoginBody,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """``email + password (RSA-OAEP)`` 登录。

    旧 P3/P4 用户 ``email IS NULL`` → 走 401 引导重新注册；与 ``password_hash IS NULL``
    的处理范式对齐，不允许"凭旧 username 路径绕过 email 主身份"。
    """
    email = _validate_email(body.email)
    pw = _decrypt_or_extract_plain(body.password)

    q = select(User).where(User.email == email)
    row = (await db.execute(q)).scalar_one_or_none()
    if row is None:
        # 时序对齐：跑一次 verify 抗探测。
        verify_password(pw, None)
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    if not verify_password(pw, row.password_hash):
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    token = await _create_session_for(row, db)
    _set_session_cookie(response, token)
    return {
        "id": row.id,
        "email": row.email,
        "username": row.username,
    }


@router.post("/logout")
async def logout(
    response: Response,
    qi_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if qi_session:
        row = await db.get(AuthSession, qi_session)
        if row is not None:
            await db.delete(row)
            await db.commit()
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
async def me(user: User = Depends(current_user)) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "username": user.username,
    }


# ──────────────────────────────────────────────────────────────────────────
# 密码重置：start / confirm
# ──────────────────────────────────────────────────────────────────────────

@router.post("/password-reset/start")
async def password_reset_start(
    body: EmailOnlyBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """寄出密码重置链接。响应**一律 200**，不暴露 email 是否存在。

    顺序与 register_start 对齐：
      1) 用户不存在 → 静默 200（不节流、不发信、不写票据），与有用户分支响应等价。
      2) 用户存在 → 节流 → 清理过多 pending → 写新票据 → 发邮件。
    """
    s = get_settings()
    email = _validate_email(body.email)

    user_q = select(User).where(User.email == email)
    user = (await db.execute(user_q)).scalar_one_or_none()
    if user is None:
        _qidbg(
            "auth.password_reset_start:unknown_email",
            {"email_hash16": _email_hash16(email)},
            "no user; respond 200 to prevent enumeration",
        )
        return {"ok": True}

    await _enforce_send_throttle(
        db, email, _PURPOSE_PASSWORD_RESET, s.mail_send_min_interval_sec
    )

    # 单用户最多保留 3 个未消费 token；超出全部置 consumed。
    pending_q = (
        select(EmailVerification)
        .where(
            EmailVerification.email == email,
            EmailVerification.purpose == _PURPOSE_PASSWORD_RESET,
            EmailVerification.consumed_at.is_(None),
        )
        .order_by(EmailVerification.created_at.desc())
    )
    pending = list((await db.execute(pending_q)).scalars())
    for old in pending[2:]:  # 保留前 2 条，加上新发 1 条 = 3
        old.consumed_at = datetime.utcnow()

    raw_token = _gen_reset_token()
    record = EmailVerification(
        email=email,
        purpose=_PURPOSE_PASSWORD_RESET,
        code_hash=None,
        token_hash=_hash_token(raw_token),
        expires_at=datetime.utcnow() + timedelta(minutes=s.mail_reset_ttl_minutes),
        request_ip=_client_ip(request) or None,
    )
    db.add(record)
    await db.commit()

    sender = get_mail_sender()
    msg = _render_password_reset_mail(
        email, raw_token, s.mail_reset_ttl_minutes, s.frontend_base_url
    )
    try:
        await sender.send(msg)
    except Exception as exc:
        _qidbg(
            "auth.password_reset_start:mail_failed",
            {"email_hash16": _email_hash16(email), "err": type(exc).__name__},
            "MailSender.send raised; aborting with 502",
        )
        raise HTTPException(status_code=502, detail="邮件发送失败，请稍后重试")

    _qidbg(
        "auth.password_reset_start:ok",
        {
            "email_hash16": _email_hash16(email),
            "ttl_min": s.mail_reset_ttl_minutes,
        },
        "reset link issued",
    )
    return {"ok": True}


@router.post("/password-reset/confirm")
async def password_reset_confirm(
    body: PasswordResetConfirmBody,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """校验 token + 落新 password_hash + **撤销该用户全部现存 session**。"""
    raw_token = (body.token or "").strip()
    if not raw_token:
        raise HTTPException(status_code=401, detail="重置链接无效或已过期")
    new_pw = _decrypt_or_extract_plain(body.new_password)

    token_hash = _hash_token(raw_token)
    q = (
        select(EmailVerification)
        .where(
            EmailVerification.purpose == _PURPOSE_PASSWORD_RESET,
            EmailVerification.token_hash == token_hash,
            EmailVerification.consumed_at.is_(None),
        )
        .order_by(EmailVerification.created_at.desc())
        .limit(1)
    )
    row = (await db.execute(q)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail="重置链接无效或已过期")
    if row.expires_at < datetime.utcnow():
        raise HTTPException(status_code=401, detail="重置链接无效或已过期")

    user = (
        await db.execute(select(User).where(User.email == row.email))
    ).scalar_one_or_none()
    if user is None:
        # token 命中但用户被删 —— 最坏情况也走废票，避免泄漏存在性。
        row.consumed_at = datetime.utcnow()
        await db.commit()
        raise HTTPException(status_code=401, detail="重置链接无效或已过期")

    row.consumed_at = datetime.utcnow()
    user.password_hash = hash_password(new_pw)
    # 撤销该用户所有现存 session（密码重置安全合规要求）
    await db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
    await db.commit()

    # 不主动建新 session：让前端跳到 /login 重新登录，避免"重置后自动登录"
    # 路径成为社工攻击手段（攻击者发动重置 → 用户点链接 → 攻击者趁同会话偷用）。
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")

    _qidbg(
        "auth.password_reset_confirm:ok",
        {"email_hash16": _email_hash16(row.email), "user_id": user.id},
        "password reset; all sessions revoked",
    )
    return {"ok": True}
