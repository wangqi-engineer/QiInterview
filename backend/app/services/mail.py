"""邮件发送抽象（P6）。

业务侧只看到一个 :class:`MailSender` 协议；具体走"控制台 / 文件桶"还是
真实 SMTP 由 :func:`get_mail_sender` 按 ``Settings.mail_backend`` 决定。

为什么不直接用 ``smtplib``？
  1. e2e 必须能在不连真实邮箱的前提下拿到验证码；
  2. 本机调试不希望每注册一次就发一封真实邮件；
  3. 生产又必须能换成真实 SMTP，且换的时候**业务代码一行都不动**。

所以走"接口 + 工厂 + env 切换"，``ConsoleMailSender`` 写文件桶
``backend/data/dev_mail/``（``.gitignore`` 里加），``SmtpMailSender`` 用
``smtplib`` + STARTTLS / SMTPS 三档（``starttls``/``ssl``/``none``）。

安全约束（与 ``rsa_keys.py`` 同口径）：
  - **OTP 原文 / reset token 原文绝不进 ``logger``**。``ConsoleMailSender``
    的 JSON 文件本身**就是邮件正文**，不算"日志泄露" —— 它在 e2e 与本机
    调试这两个场景里就是替代邮件邮箱的"收件箱"。
  - ``SmtpMailSender`` 对 SMTP 凭据只在内存里持有，``__repr__`` 屏蔽密码字段。
  - ``QI_MAIL_DRY_RUN=1`` 全局兜底：即便误把 ``MAIL_BACKEND=smtp`` 上了也
    不会真正联网，写一条 warning 后落控制台桶。供 CI 快速救命。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import smtplib
import ssl
import time
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Optional, Protocol

from app.config import BACKEND_ROOT, get_settings


logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class MailMessage:
    """统一的"一封待发邮件"。``html`` 可选；``meta`` 仅作埋点元数据。"""

    to: str
    subject: str
    text: str
    html: Optional[str] = None
    # 业务侧塞 ``{"purpose": "register", "expires_in_min": 10}`` 之类，
    # ConsoleMailSender 会原样落 JSON，便于 e2e helper 反查。
    meta: dict = field(default_factory=dict)


class MailSender(Protocol):
    """异步邮件发送协议。所有实现都必须满足"幂等失败 = 抛异常"语义。"""

    async def send(self, msg: MailMessage) -> None: ...


# ──────────────────────────────────────────────────────────────────────────
# Console / 文件桶实现
# ──────────────────────────────────────────────────────────────────────────

# 默认桶位置（也允许通过 env 覆盖；测试 fixture 会塞临时目录进来）。
_DEFAULT_DEV_MAIL_DIR = BACKEND_ROOT / "data" / "dev_mail"


def _resolve_dev_mail_dir() -> Path:
    override = os.environ.get("QI_DEV_MAIL_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return _DEFAULT_DEV_MAIL_DIR


class ConsoleMailSender:
    """把每封邮件序列化成 JSON 落 ``backend/data/dev_mail/<...>.json``。

    文件名形如 ``20260429T011500_register_3a7c.json``，按字典序天然时间序，
    e2e helper 直接 ``glob('*_register_*.json')`` 找最新一封。
    """

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self._base = (base_dir or _resolve_dev_mail_dir()).resolve()
        self._base.mkdir(parents=True, exist_ok=True)

    @property
    def base_dir(self) -> Path:
        return self._base

    async def send(self, msg: MailMessage) -> None:
        ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        purpose = (msg.meta.get("purpose") or "generic").strip() or "generic"
        short = uuid.uuid4().hex[:6]
        fname = f"{ts}_{purpose}_{short}.json"
        target = self._base / fname
        payload = {
            "to": msg.to,
            "subject": msg.subject,
            "text": msg.text,
            "html": msg.html,
            "meta": msg.meta,
            "saved_at_ms": int(time.time() * 1000),
        }
        # 同步写文件 + 走 to_thread 避免在 event loop 上阻塞。
        await asyncio.to_thread(
            target.write_text, json.dumps(payload, ensure_ascii=False, indent=2), "utf-8"
        )
        logger.info(
            "ConsoleMailSender wrote %s (to=%s, purpose=%s)",
            target.name,
            msg.to,
            purpose,
        )


# ──────────────────────────────────────────────────────────────────────────
# SMTP 实现
# ──────────────────────────────────────────────────────────────────────────

class SmtpMailSender:
    """``smtplib`` + STARTTLS/SMTPS 真实 SMTP 实现。

    ``smtp_security`` 三档：
      * ``starttls`` —— 连 587，``EHLO`` → ``STARTTLS`` → ``EHLO`` → ``LOGIN``；
      * ``ssl`` —— 连 465，``SMTP_SSL`` 直接 TLS over TCP；
      * ``none`` —— 仅本机调试，不上 TLS，**生产严禁**，warning 一行。

    所有同步阻塞 IO 走 ``asyncio.to_thread`` 委托给线程池。
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        mail_from: str,
        security: str = "starttls",
    ) -> None:
        if not host or not mail_from:
            raise RuntimeError(
                "SmtpMailSender 需要 SMTP_HOST + MAIL_FROM 都非空，"
                "请检查 .env.local / 环境变量。"
            )
        self._host = host
        self._port = int(port)
        self._user = user
        # 不在 __repr__ / 日志里出现的位置即可；密码本体仍要在 send 时使用。
        self._password = password
        self._mail_from = mail_from
        self._security = security.strip().lower() or "starttls"

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"SmtpMailSender(host={self._host}, port={self._port}, "
            f"user={self._user!r}, security={self._security}, "
            f"mail_from={self._mail_from!r}, password=***)"
        )

    async def send(self, msg: MailMessage) -> None:
        if os.environ.get("QI_MAIL_DRY_RUN", "").strip().lower() in ("1", "true", "yes"):
            logger.warning(
                "QI_MAIL_DRY_RUN=1 拦截 SmtpMailSender；改写到 ConsoleMailSender。"
            )
            await ConsoleMailSender().send(msg)
            return

        em = EmailMessage()
        em["From"] = formataddr(("QiInterview", self._mail_from))
        em["To"] = msg.to
        em["Subject"] = msg.subject
        em.set_content(msg.text)
        if msg.html:
            em.add_alternative(msg.html, subtype="html")

        await asyncio.to_thread(self._send_sync, em)

    def _send_sync(self, em: EmailMessage) -> None:
        ctx = ssl.create_default_context()
        if self._security == "ssl":
            with smtplib.SMTP_SSL(self._host, self._port, context=ctx, timeout=15) as cli:
                if self._user:
                    cli.login(self._user, self._password)
                cli.send_message(em)
        elif self._security == "starttls":
            with smtplib.SMTP(self._host, self._port, timeout=15) as cli:
                cli.ehlo()
                cli.starttls(context=ctx)
                cli.ehlo()
                if self._user:
                    cli.login(self._user, self._password)
                cli.send_message(em)
        elif self._security == "none":
            logger.warning(
                "SmtpMailSender security=none：邮件以明文走 SMTP，仅供本机调试。"
            )
            with smtplib.SMTP(self._host, self._port, timeout=15) as cli:
                if self._user:
                    cli.login(self._user, self._password)
                cli.send_message(em)
        else:
            raise RuntimeError(
                f"SmtpMailSender 不认识的 security 档位：{self._security!r}；"
                "应为 starttls / ssl / none 之一。"
            )


# ──────────────────────────────────────────────────────────────────────────
# 工厂
# ──────────────────────────────────────────────────────────────────────────

_INSTANCE: Optional[MailSender] = None


def get_mail_sender() -> MailSender:
    """按 ``Settings.mail_backend`` 选 ``console|smtp`` 实现，进程内单例。

    测试 fixture 通过 :func:`set_mail_sender` 注入自定义实现，避免修改
    ``Settings``。
    """
    global _INSTANCE
    if _INSTANCE is not None:
        return _INSTANCE
    s = get_settings()
    backend = (s.mail_backend or "console").strip().lower()
    if backend == "smtp":
        _INSTANCE = SmtpMailSender(
            host=s.smtp_host,
            port=s.smtp_port,
            user=s.smtp_user,
            password=s.smtp_password,
            mail_from=s.mail_from,
            security=s.smtp_security,
        )
    elif backend == "console":
        _INSTANCE = ConsoleMailSender()
    else:
        raise RuntimeError(
            f"未知 MAIL_BACKEND={backend!r}；目前仅支持 console / smtp。"
        )
    return _INSTANCE


def set_mail_sender(sender: Optional[MailSender]) -> None:
    """测试 / fixture 专用：替换/清空全局单例。"""
    global _INSTANCE
    _INSTANCE = sender


__all__ = [
    "MailMessage",
    "MailSender",
    "ConsoleMailSender",
    "SmtpMailSender",
    "get_mail_sender",
    "set_mail_sender",
]
