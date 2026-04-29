"""Phase 5 / P0：登录 / 注册密码 RSA-OAEP 端到端加密的密钥管理。

设计要点：

  - **密钥对单例**：服务进程首次需要时按需生成一对 2048-bit RSA 密钥
    （``RSA-OAEP / SHA-256`` 签名/验证算法对配；客户端用 SHA-256 OAEP
    加密一次密码）。私钥落 ``backend/data/auth_rsa.pem``（PKCS#8 PEM，
    ``mode=0o600``，仅本进程账户可读）；公钥派生缓存到内存，由
    ``GET /api/auth/pubkey`` 直接派发出去。
  - **持久化** > 重启重生：进程重启如能读到旧 PEM 就直接复用，避免历史
    前端拿到的 sessionStorage 缓存的 pubkey 突然失配。
  - **冲突处理**：PEM 文件存在但解析失败（被 trunc / 被覆写） → 自动
    重新生成并覆盖；只在最严重的写入失败时抛 ``RuntimeError``，让
    fastapi startup 直接红，从而把 P0 安全问题暴露给运维。
  - **绝不日志泄露**：``_qidbg`` 只记 ``elapsed_ms`` / ``ciphertext_len`` /
    ``key_path`` 等元信息，密码原文 / 明文私钥 / 完整密钥指纹都不允许
    出现在任何日志通道。

外部 API（仅 3 个）：
  - :func:`get_public_pem`：返回 PEM 格式公钥字符串（SubjectPublicKeyInfo）。
  - :func:`get_public_fingerprint`：公钥 SHA-256 指纹的前 16 个 hex char，
    供前端 / 测试用作"是否拿到了同一把公钥"的弱校验。
  - :func:`decrypt_password`：把 base64 RSA-OAEP 密文解成明文密码。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


# 默认 2048-bit。1024 已经被现代浏览器视为弱算法；4096 在登录路径上 ~80ms
# 解密成本明显偏重。2048 是 RSA-OAEP 的合规甜点。
_KEY_BITS = 2048
_PUBLIC_EXPONENT = 65537


# Path(__file__) = backend/app/core/rsa_keys.py
#   .parent .parent          -> backend/app
#   .parent .parent .parent   -> backend
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent  # backend/
_REPO_ROOT = _BACKEND_DIR.parent  # repo root (.cursor lives here)
_DEFAULT_KEY_PATH = _BACKEND_DIR / "data" / "auth_rsa.pem"


@dataclass
class _KeyMaterial:
    private: rsa.RSAPrivateKey
    public_pem: str
    fingerprint: str  # 公钥 SPKI DER 的 sha256 前 16 hex char


_LOCK = threading.RLock()
_CACHE: Optional[_KeyMaterial] = None


def _qidbg(location: str, data: dict, message: str = "") -> None:
    """与 ``passwords.py`` / ``voice_ws.py`` 同口径的 ndjson 埋点。
    严格只记元信息：``location`` / ``data``（不含密码原文 / 明文私钥 / 全
    密钥指纹）/ ``timestamp``。"""
    try:
        path = _REPO_ROOT / ".cursor" / "debug-714cc8.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "sessionId": "P5-RSA",
                        "runId": "be_qidbg",
                        "hypothesisId": "P5-AUTH-RSA-OAEP",
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


def _key_path() -> Path:
    """允许通过 ``QI_AUTH_RSA_KEY_PATH`` 环境变量覆盖密钥文件位置；默认
    放在 ``backend/data/auth_rsa.pem``。"""
    override = os.environ.get("QI_AUTH_RSA_KEY_PATH")
    return Path(override).expanduser().resolve() if override else _DEFAULT_KEY_PATH


def _derive_public(private: rsa.RSAPrivateKey) -> tuple[str, str]:
    """从私钥导出 (PEM SubjectPublicKeyInfo, 公钥 fingerprint16hex)。"""
    public_obj = private.public_key()
    spki_der = public_obj.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pem = public_obj.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    fp = hashlib.sha256(spki_der).hexdigest()[:16]
    return pem, fp


def _load_or_generate() -> _KeyMaterial:
    """读 PEM 失败 → 生成新对覆盖。**此函数必须在 ``_LOCK`` 下调用**。"""
    kp = _key_path()
    kp.parent.mkdir(parents=True, exist_ok=True)

    # 1) 尝试读已有 PEM
    if kp.exists():
        try:
            data = kp.read_bytes()
            private = serialization.load_pem_private_key(
                data, password=None, backend=default_backend()
            )
            if not isinstance(private, rsa.RSAPrivateKey):
                raise TypeError(
                    f"auth_rsa.pem 不是 RSA 私钥而是 {type(private).__name__}"
                )
            pub_pem, fp = _derive_public(private)
            _qidbg(
                "rsa.bootstrap:loaded",
                {
                    "key_bits": private.key_size,
                    "key_path": str(kp),
                    "fingerprint16": fp,
                },
                "loaded existing private key",
            )
            return _KeyMaterial(private=private, public_pem=pub_pem, fingerprint=fp)
        except Exception as exc:
            # 文件格式坏了 / 上次写到一半 / 算法不对 —— 兜底再生成一份。
            _qidbg(
                "rsa.bootstrap:reload_failed",
                {"key_path": str(kp), "err": f"{type(exc).__name__}: {exc}"},
                "stored PEM unusable, regenerating",
            )

    # 2) 生成
    t0 = time.monotonic()
    private = rsa.generate_private_key(
        public_exponent=_PUBLIC_EXPONENT,
        key_size=_KEY_BITS,
        backend=default_backend(),
    )
    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

    pem_bytes = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    # 3) 落盘 + 收紧权限（POSIX；Windows 上 0o600 是 no-op，由 NTFS ACL 决定，
    #    至少明确写出我们期望的 mode）。
    tmp_path = kp.with_suffix(kp.suffix + ".tmp")
    tmp_path.write_bytes(pem_bytes)
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, kp)

    pub_pem, fp = _derive_public(private)
    _qidbg(
        "rsa.bootstrap:generated",
        {
            "key_bits": _KEY_BITS,
            "elapsed_ms": elapsed_ms,
            "key_path": str(kp),
            "fingerprint16": fp,
        },
        "generated new RSA-2048 key pair",
    )
    return _KeyMaterial(private=private, public_pem=pub_pem, fingerprint=fp)


def _ensure_loaded() -> _KeyMaterial:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    with _LOCK:
        if _CACHE is None:
            _CACHE = _load_or_generate()
        return _CACHE


def get_public_pem() -> str:
    """返回 PEM 格式公钥（SubjectPublicKeyInfo，文本，``-----BEGIN PUBLIC KEY-----``
    打头），可直接发给前端用 ``crypto.subtle.importKey('spki', ...)``。"""
    return _ensure_loaded().public_pem


def get_public_fingerprint() -> str:
    """前 16 hex char 的 SHA-256 公钥指纹。"""
    return _ensure_loaded().fingerprint


def decrypt_password(ciphertext_b64: str) -> str:
    """OAEP 解密，返回 utf-8 明文密码。出错抛 ``ValueError`` —— 调用方应
    把它转成 401 ``invalid encrypted credential`` 而非 500，避免泄漏密钥
    层错误细节。"""
    if not isinstance(ciphertext_b64, str) or not ciphertext_b64:
        raise ValueError("RSA ciphertext must be a non-empty base64 string")

    # base64 解码：兼容前端可能的 padding 缺失。
    s = ciphertext_b64.strip()
    pad = (-len(s)) % 4
    if pad:
        s = s + ("=" * pad)
    try:
        ct = base64.b64decode(s, validate=False)
    except Exception as exc:
        raise ValueError(f"RSA ciphertext base64 decode failed: {exc}") from exc

    km = _ensure_loaded()
    t0 = time.monotonic()
    try:
        plain = km.private.decrypt(
            ct,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
    except Exception as exc:
        # 不要把 cryptography 的内部异常文本回传：可能含中间状态信息。
        _qidbg(
            "rsa.decrypt:fail",
            {
                "ct_len_bytes": len(ct),
                "err": type(exc).__name__,
                "fingerprint16": km.fingerprint,
            },
            "OAEP decrypt raised",
        )
        raise ValueError("invalid encrypted credential") from exc

    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
    _qidbg(
        "rsa.decrypt:done",
        {
            "ct_len_bytes": len(ct),
            "plain_len": len(plain),
            "elapsed_ms": elapsed_ms,
            "fingerprint16": km.fingerprint,
        },
        "OAEP decrypt ok",
    )
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("decrypted password is not valid utf-8") from exc


__all__ = [
    "decrypt_password",
    "get_public_fingerprint",
    "get_public_pem",
]
