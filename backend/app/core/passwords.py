"""密码哈希 / 校验 —— 纯标准库 PBKDF2-SHA256（避免引入 bcrypt / argon2 依赖）。

格式：``pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>``。

P4 / a4 用户合同：『密码不能被泄露 / 做好安全校验』。具体做法：
  - **不存明文**：``hash_password`` 永远只输出哈希字符串，明文密码只在
    内存中短暂存在，绝不进 ORM 模型 / 日志 / response。
  - **constant-time compare**：``hmac.compare_digest`` 抗时序侧信道。
  - **每条独立 salt**：16 字节随机 salt，相同密码不同用户哈希不同。
  - **iteration 200 000**：与 Django 4.x 默认值相当；2026 年 CPU 上每条 ~50 ms，
    暴力破解成本足够高，又不至于让 register/login 接口超时。

埋点：``passwords.hash:done`` / ``passwords.verify:done`` 只记录
elapsed_ms / format_version，绝不记录密码原文或哈希字节。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Final

_FORMAT: Final[str] = "pbkdf2_sha256"
_ITER_DEFAULT: Final[int] = 200_000
_SALT_BYTES: Final[int] = 16
_HASH_BYTES: Final[int] = 32  # SHA-256 digest size


def _qidbg(location: str, data: dict, message: str = "") -> None:
    """与 voice_ws._qidbg 同口径的最小日志：写到 .cursor/debug-714cc8.log。

    严格只记元信息（elapsed_ms / iterations / ok / format），不写明文 / 哈希。
    """
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
                        "sessionId": "PASSWD",
                        "runId": "be_qidbg",
                        "hypothesisId": "P4-AUTH-PASSWORD",
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


def hash_password(password: str, *, iterations: int = _ITER_DEFAULT) -> str:
    """对明文密码做 PBKDF2-SHA256，返回 self-describing 字符串。"""
    if not isinstance(password, str) or not password:
        raise ValueError("password 必须是非空字符串")

    t0 = time.monotonic()
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
        dklen=_HASH_BYTES,
    )
    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
    _qidbg(
        "passwords.hash:done",
        {"format": _FORMAT, "iterations": iterations, "elapsed_ms": elapsed_ms},
        "hashed password",
    )
    return f"{_FORMAT}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, hashed: str | None) -> bool:
    """常数时间校验。``hashed`` 为 ``None`` / 空 / 解析失败 → 返回 False。

    特别强调：``hashed is None`` 直接 False 是 P4 的硬约束 —— lite-auth 时期
    注册的"无密码用户"必须重设密码才能再登录，不能凭"两边都没密码"绕过。
    """
    if not isinstance(password, str) or not password or not hashed:
        return False

    try:
        fmt, iter_s, salt_hex, hash_hex = hashed.split("$")
    except ValueError:
        _qidbg(
            "passwords.verify:bad_format",
            {"hashed_len": len(hashed)},
            "hashed string does not match expected format",
        )
        return False
    if fmt != _FORMAT:
        return False
    try:
        iterations = int(iter_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False

    t0 = time.monotonic()
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
        dklen=len(expected),
    )
    ok = hmac.compare_digest(digest, expected)
    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
    _qidbg(
        "passwords.verify:done",
        {
            "format": fmt,
            "iterations": iterations,
            "ok": ok,
            "elapsed_ms": elapsed_ms,
        },
        "verified password",
    )
    return ok


__all__ = ["hash_password", "verify_password"]


# ── self-test：模块 import 时跑一次轻量自检（dev / e2e 阶段足够），避免格式
#   解析回归引入静默 401。生产可通过 QI_DISABLE_PASSWORD_SELFTEST=1 关闭。
if not os.environ.get("QI_DISABLE_PASSWORD_SELFTEST"):
    try:
        _h = hash_password("__qi_selftest__", iterations=1_000)
        assert verify_password("__qi_selftest__", _h), "passwords self-test failed"
        assert not verify_password("wrong", _h), "passwords self-test (negative) failed"
    except Exception as exc:  # pragma: no cover
        _qidbg("passwords.selftest:fail", {"err": str(exc)}, "self-test failure")
        raise
