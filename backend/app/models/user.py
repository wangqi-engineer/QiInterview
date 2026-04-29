"""用户、Session（cookie 鉴权）、邮箱验证票据、用户凭据 ORM 模型。

P3 / lite-auth → P4（密码） → P6（邮箱主身份）演进：
  - ``User`` ——
      * P3：``username`` 唯一作为身份；
      * P4：新增 ``password_hash``（PBKDF2-SHA256 + 16 字节 salt + 200000 轮，
        纯 stdlib 实现，避免引入 bcrypt/argon2 依赖）；
      * P6：新增 ``email`` 列承担"主身份 + 找回密码"双重职责。新注册用户
        ``email`` 必填且唯一；旧 lite-auth/P4 用户允许 ``email IS NULL``，
        但 ``/login`` 路径会拦下让其重新注册（与 ``password_hash`` 的处理
        范式对齐）。``username`` 退化为可选昵称，唯一约束放宽（允许 NULL，
        但若提供则保持唯一以维持老 ``e2e_default_<uuid>`` 命名习惯）。
        ``email_verified_at`` 仅作审计冗余，业务路径不依赖（注册要求 OTP 通过
        才能落 ``User`` 行）。
  - ``Session`` —— cookie 内容只是一个 32-字节 hex token；过期时间 30 天。
  - ``EmailVerification`` —— 注册 OTP 与密码重置 token 的统一票据表。
    ``code_hash``（注册）/``token_hash``（重置）只落 ``sha256``，原文走邮件
    一次性派发，落库 / 落日志全是哈希；``purpose`` 区分流；``consumed_at``
    实现一次性语义；``request_ip`` 仅供未来反爆破，业务路径暂不读。
  - ``UserCredential`` —— 每用户一行，存 LLM/语音相关 key。这里写到关系数据库
    是为了让 ``GET /api/credentials`` 在多设备 / 重启后仍能取回（前端 zustand
    在 page reload 后会丢失内存敏感字段，所以需要后端持久化用户隔离的拷贝）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # P6：``username`` 退化为可选昵称。SQLite 的 UNIQUE 在多个 NULL 行之间
    # 视为不冲突（与 SQL 标准一致），所以这里允许 NULL 同时保持 unique，老
    # ``e2e_default_<uuid>`` 命名仍然防撞。
    username: Mapped[Optional[str]] = mapped_column(
        String(80), unique=True, index=True, nullable=True
    )
    # P6：邮箱作为主身份。新注册路径强制非空 + 唯一；老 lite-auth / P4 用户
    # 允许 NULL（兼容期），``/login`` 检查到 ``email IS NULL`` → 401 引导
    # 重新注册并绑定。此处 SQLAlchemy 列定义保持 nullable=True，业务约束在
    # 应用层强制（避免 ALTER TABLE 在 SQLite 上加 NOT NULL 失败）。
    email: Mapped[Optional[str]] = mapped_column(
        String(254), unique=True, index=True, nullable=True
    )
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )
    # P4 / a4：PBKDF2 哈希字符串，格式 "pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>"
    # 旧用户（lite-auth 时期注册的）此列为空 → 强制 401，逼用户重新注册。
    password_hash: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    sessions: Mapped[list["Session"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    credential: Mapped[Optional["UserCredential"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )


class Session(Base):
    __tablename__ = "auth_session"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped[User] = relationship(back_populates="sessions")


class UserCredential(Base):
    """每用户一行的 API key 缓存；GET/PUT /api/credentials 的存储侧。"""

    __tablename__ = "user_credential"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    llm_provider: Mapped[str] = mapped_column(String(20), default="doubao")
    llm_key: Mapped[str] = mapped_column(Text, default="")
    llm_model: Mapped[str] = mapped_column(String(120), default="")
    llm_model_fast: Mapped[str] = mapped_column(String(120), default="")
    llm_model_deep: Mapped[str] = mapped_column(String(120), default="")

    # v0.4 新合同：火山引擎语音 ``api/v3/tts/unidirectional`` 与
    # ``api/v3/sauc/bigmodel_async`` 共用的单 ``X-Api-Key``。其它 ``voice_*``
    # 字段在新业务流里都不会被读，仅为不破坏老 schema 保留。
    volc_voice_key: Mapped[str] = mapped_column(Text, default="")

    # ── 历史字段（向后兼容；业务代码不再读取）──
    dashscope_key: Mapped[str] = mapped_column(Text, default="")
    voice_app_id: Mapped[str] = mapped_column(String(80), default="")
    voice_token: Mapped[str] = mapped_column(Text, default="")
    voice_tts_app_id: Mapped[str] = mapped_column(String(80), default="")
    voice_tts_token: Mapped[str] = mapped_column(Text, default="")
    voice_stt_app_id: Mapped[str] = mapped_column(String(80), default="")
    voice_stt_token: Mapped[str] = mapped_column(Text, default="")
    voice_tts_rid: Mapped[str] = mapped_column(String(120), default="")
    voice_asr_rid: Mapped[str] = mapped_column(String(120), default="")

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="credential")


class EmailVerification(Base):
    """邮箱验证票据（注册 OTP + 密码重置 token 共用）。

    设计要点：
      - **purpose** 二选一：``register`` / ``password_reset``。同表两用，靠
        purpose 索引下选最新未 consumed 行。
      - **代码原文 / token 原文绝不入库 / 入日志**：``code_hash`` =
        sha256(6 位 OTP)；``token_hash`` = sha256(``secrets.token_urlsafe(32)``)。
        原文走邮件一次性派发；DB 漏库等价于"被告知有过验证发生"，无法
        反推出可使用的票据。
      - **一次性语义**：``consumed_at IS NOT NULL`` 即视为废票；
        ``/register/verify`` 与 ``/password-reset/confirm`` 都在事务内置位。
      - **request_ip** 仅做埋点用，未来想做 IP 维度限流时不需要再迁库。
    """

    __tablename__ = "email_verification"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(254), index=True, nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    # 注册路径 = 6 位 OTP 的 sha256 hex；重置路径留空
    code_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, default=None)
    # 重置路径 = 32-byte URL-safe token 的 sha256 hex；注册路径留空
    token_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, default=None)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None
    )
    request_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


__all__ = ["User", "Session", "EmailVerification", "UserCredential"]
