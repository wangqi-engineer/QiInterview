"""FastAPI 应用入口。"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.api import auth, credentials, interview, jobs, reports, resume, voice_ws
from app.config import get_settings
from app.db.session import init_db
from app.services.jobs.refresher import (
    start_scheduler,
    stop_scheduler,
    warmup_if_empty,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    start_scheduler()
    # 启动后异步暖一下岗位库（不阻塞 startup）
    import asyncio

    asyncio.create_task(warmup_if_empty())

    # P5 / Phase 5：在 startup 阶段就把 RSA-OAEP 密钥对加载/生成出来。
    # 这样 ``GET /api/auth/pubkey`` 不会在请求路径上触发首次 PEM IO；
    # 同时若 ``backend/data/auth_rsa.pem`` 损坏会立即在 startup 日志里
    # 看到 ``rsa.bootstrap:reload_failed`` → ``:generated`` 的对偶事件。
    try:
        from app.core import rsa_keys

        _ = rsa_keys.get_public_pem()
        logger.info(
            "RSA auth pubkey ready (fingerprint16=%s)",
            rsa_keys.get_public_fingerprint(),
        )
    except Exception as exc:
        logger.error("RSA auth pubkey bootstrap failed: %s", exc)
        raise

    # v0.4 起 TTS 已切到火山 ``api/v3/tts/unidirectional``（HTTP POST）+ 单
    # ``X-Api-Key``，连接靠 ``httpx.AsyncClient`` 自动复用，不再有 5–10 s
    # 的 WS 握手；同时业务侧严禁回退到 env 凭据，启动期根本拿不到首位用户
    # 会用的 key —— 旧的 ``tts_pool.warmup_keys`` 与 ``warmup_filler_audio_cache``
    # 双重热身在新架构下既无收益也无意义，整段从 lifespan 移除。
    # ``app/services/tts_pool.py`` 仍保留 ``pool`` 单例作 no-op，避免破坏
    # ``voice_ws`` 那边的旧调用点。

    # D12 修复（VOICE-E2E-LATENCY）—— LLM 端：实测 ``after_create_ms`` 高达
    # 7.9s（test_i8 日志），等价于每次 ``client.chat.completions.create()``
    # 都走一次 TLS 握手到 ``ark.cn-beijing.volces.com``。``llm_pool`` 单例化
    # ``AsyncOpenAI``（httpx keepalive_expiry=300s），并在启动期发一次
    # ``max_tokens=1`` 的小请求，把 TLS 握手开销前置；后台再 60s ping 一次
    # 维持长连接。如果环境无 ``ARK_API_KEY``（mock 模式 / 单元测试），跳过。
    try:
        from app.core.credentials import LLMCreds
        from app.services.llm_mock import is_mock_enabled as _llm_mock_check
        from app.services.llm_pool import pool as llm_pool

        s2 = get_settings()
        if s2.ark_api_key and not _llm_mock_check():
            llm_creds = LLMCreds(
                provider=s2.llm_provider,
                api_key=s2.ark_api_key,
                model=s2.llm_model,
                model_fast=s2.llm_model_fast or "",
                model_deep=s2.llm_model_deep or "",
            )
            asyncio.create_task(llm_pool.warmup(llm_creds))
            llm_pool.schedule_keepalive(llm_creds)
            logger.info("LLM client pool warmup scheduled (provider=%s)", llm_creds.provider)
    except Exception as exc:
        logger.warning("LLM client pool startup failed: %s", exc)

    try:
        yield
    finally:
        stop_scheduler()
        # tts_pool.shutdown 在 v0.4 是 no-op；保留 import 形式只是为了若
        # 未来有人再开 warm 池能立刻挂回来。
        try:
            from app.services.tts_pool import pool as tts_pool

            await tts_pool.shutdown()
        except Exception:
            pass
        try:
            from app.services.llm_pool import pool as llm_pool

            await llm_pool.shutdown()
        except Exception:
            pass


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """P6：固定 + 可选的传输安全响应头。

    所有环境都加：
      - ``X-Content-Type-Options: nosniff`` —— 禁掉 MIME sniff，避免上传伪
        内容被当成可执行脚本。
      - ``Referrer-Policy: strict-origin-when-cross-origin`` —— 跨站只发
        origin 不发 path/query，避免 reset URL 里的 ``?token=...`` 因 referer
        泄漏到第三方 CDN / 分析脚本。
      - ``X-Frame-Options: DENY`` —— 防 clickjacking 套娃登录页。

    仅 ``app_env == prod`` 追加：
      - ``Strict-Transport-Security`` 一年期 + ``includeSubDomains`` —— 浏览器
        缓存"只走 HTTPS"，把 SSL Strip 攻击从可达变为长期不可达。
      - 一份基础 ``Content-Security-Policy``，仅 self / inline-style；script
        允许 self + inline（dev 还需要 vite HMR，所以 prod 才开）。
    dev 模式不发 HSTS 是为了避免一台开发机被永久"钉死"在某个 localhost +
    HTTP 黑洞里 —— 浏览器对 HSTS 的本地缓存清不太干净。
    """

    def __init__(self, app, *, prod: bool) -> None:
        super().__init__(app)
        self._prod = bool(prod)

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        if self._prod:
            resp.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
            resp.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; "
                "media-src 'self' blob:; "
                "connect-src 'self' wss: https:; "
                "frame-ancestors 'none';",
            )
        return resp


def _enforce_prod_safety(settings) -> None:  # noqa: ANN001
    """``app_env == prod`` 时必备清单。任一项不达标 → ``RuntimeError``，让
    fastapi startup 直接红、运维必须显式配齐再上线。"""
    env = (settings.app_env or "dev").strip().lower()
    if env != "prod":
        return
    bad: list[str] = []
    if not settings.cookie_secure:
        bad.append(
            "COOKIE_SECURE=true 未设置 —— 生产 https 部署必须启用，否则 cookie 会"
            "在 HTTP 网段裸奔"
        )
    hosts = [h for h in (settings.allowed_hosts or []) if h]
    if not hosts or hosts == ["*"]:
        bad.append(
            "ALLOWED_HOSTS 必须显式列出域名（不能是 *），否则 Host header injection "
            "可能让 reset URL 拼到攻击者域上"
        )
    cors = [o for o in (settings.cors_origins or []) if o]
    if not cors or "*" in cors:
        bad.append(
            "CORS_ORIGINS 必须显式列出前端 origin（不能含 *），否则配合 credentials=true"
            "等于把任意第三方网页都允许带 cookie 调本服务"
        )
    if bad:
        raise RuntimeError(
            "APP_ENV=prod 启动期安全检查失败：\n  - " + "\n  - ".join(bad)
        )


def create_app() -> FastAPI:
    settings = get_settings()
    from app.services.llm_mock import is_mock_enabled

    if is_mock_enabled() and "PYTEST_VERSION" not in os.environ:
        logger.warning(
            "LLM 处于 Mock 模式（QI_LLM_MOCK）。若需真实豆包/方舟，请在仓库根 .env.local 设 "
            "QI_LLM_MOCK=0 并清掉 shell 里残留的 QI_LLM_MOCK=1 后启动后端，或使用 scripts/dev.ps1。"
        )
    # P6：生产模式启动期硬校验。开发模式不触发。
    _enforce_prod_safety(settings)

    app = FastAPI(
        title="QiInterview Backend",
        version="0.1.0",
        description="QiInterview 智能面试系统后端",
        lifespan=lifespan,
    )

    # P6：TrustedHostMiddleware —— 任何 Host header 不在白名单的请求直接 400。
    # dev 默认 ``["*"]`` 完全放过；prod 必须显式列域名（_enforce_prod_safety 兜守）。
    allowed = [h for h in (settings.allowed_hosts or []) if h] or ["*"]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed)

    # P3 / lite-auth：cookie 鉴权要求 ``allow_credentials=True``，而 CORS
    # 规范在 ``allow_origins=["*"]`` + credentials=True 时无效；这里收紧为
    # 显式 origin 列表（包含 .env 配置 + 前端 dev/prod 的常见来源），并把
    # 主机替换成 host:port 通配。
    cors_origins = list(dict.fromkeys(settings.cors_origins + [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ]))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # P6：传输安全响应头。prod 模式追加 HSTS / CSP；dev 仅基础三连。
    is_prod = (settings.app_env or "dev").strip().lower() == "prod"
    app.add_middleware(SecurityHeadersMiddleware, prod=is_prod)

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "version": "0.1.0"}

    app.include_router(auth.router, prefix="/api")
    app.include_router(credentials.router, prefix="/api")
    app.include_router(interview.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")
    app.include_router(resume.router, prefix="/api")
    app.include_router(reports.router, prefix="/api")
    app.include_router(voice_ws.router)  # /ws/...
    return app


app = create_app()
