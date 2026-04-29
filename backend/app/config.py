"""全局配置。

字段分两类：
  1. **服务侧偏好**：DB / CORS / 评分阈值 / 音色路由 / 调度器周期。这些
     可以放心从 ``.env.local`` 读，本来就是部署级配置。
  2. **业务凭据**（``ark_api_key`` / ``llm_*`` / ``volc_voice_key`` / 旧
     ``volc_*_app_id|access_token`` / ``dashscope_api_key``）：仍然在这里
     声明 + 解析 env，**但仅供 e2e 脚本读取后再注入前端 UI 触发"前端透传"
     路径**。`backend/app/core/credentials.py` 已经把所有 REST/WS 凭据依赖
     函数对这些字段的回退路径全部删掉，业务代码不再读取它们。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=[
            BACKEND_ROOT.parent / ".env.local",
            BACKEND_ROOT.parent / ".env",
        ],
        env_file_encoding="utf-8",
        extra="ignore",
    )

    backend_host: str = "127.0.0.1"
    backend_port: int = 8000

    database_url: str = Field(
        default=f"sqlite+aiosqlite:///{(DATA_DIR / 'qiinterview.db').as_posix()}"
    )

    jobs_refresh_interval_hours: int = 6

    # ─── e2e-only 业务凭据（业务代码不再回退读取） ───
    # 仅供 ``tests/e2e/conftest.py`` 与 ``test_*.py`` 取出来再 fill 到前端
    # UI / WS auth 首帧。生产部署可以全部留空。
    ark_api_key: str = ""
    llm_provider: str = "doubao"
    llm_model: str = "doubao-seed-1-6-251015"
    llm_model_fast: str = ""
    llm_model_deep: str = ""
    # 火山新接口的单 X-Api-Key（`api/v3/tts/unidirectional` 与
    # `api/v3/sauc/bigmodel_async` 共用）。e2e 走 .env.local，生产不读。
    volc_voice_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "VOLC_VOICE_KEY",
            "VOLC_TTS_API_KEY",
            "VOLC_STT_API_KEY",
        ),
    )
    # ── 历史火山字段：仅为不删除老 e2e fixture / DB 字段保留；业务代码不读 ──
    volc_audio_app_id: str = ""
    volc_audio_access_token: str = ""
    volc_tts_app_id: str = Field(
        default="",
        validation_alias=AliasChoices("VOLC_TTS_APPID", "VOLC_TTS_APP_ID"),
    )
    volc_tts_access_token: str = Field(
        default="",
        validation_alias=AliasChoices("VOLC_TTS_ACCESS_TOKEN"),
    )
    volc_stt_app_id: str = Field(
        default="",
        validation_alias=AliasChoices("VOLC_STT_APPID", "VOLC_STT_APP_ID"),
    )
    volc_stt_access_token: str = Field(
        default="",
        validation_alias=AliasChoices("VOLC_STT_ACCESS_TOKEN"),
    )
    # 旧版双向流式接口的 resource id；新接口在代码内常量里固定，不再读这两条。
    volc_tts_resource_id: str = "volc.service_type.10029"
    volc_asr_resource_id: str = "volc.bigasr.sauc.duration"

    # 历史 DashScope key —— 业务侧已不读；保留只为 ``UserCredential`` schema
    # 里同名列与 e2e 脚本不需要改。
    dashscope_api_key: str = ""

    # ─── 服务侧偏好：面试官音色按面试类型路由 ───
    # 留空走 ``app/core/voice_router.py`` 内置的 VOICE_MAP；填了就覆盖。
    # 用于在不改代码的前提下调整面试官人设。
    #
    # 命名兼容：业务字段名按面试类型 ``tech1/tech2/comprehensive/hr``；
    # ``.env.local`` 既可以用短名（``VOLC_VOICE_TECH1`` 等），也可以用
    # 用户合同的长名（``VOLC_VOICE_TYPE_FIRST_ROUND_INTERVIEWER`` 等），
    # 两者通过 ``AliasChoices`` 等价。tech1 = FIRST_ROUND；
    # tech2 = SECOND_ROUND；comprehensive = THIRD_ROUND；hr = HR。
    volc_voice_tech1: str = Field(
        default="",
        validation_alias=AliasChoices(
            "VOLC_VOICE_TECH1",
            "VOLC_VOICE_TYPE_FIRST_ROUND_INTERVIEWER",
        ),
    )
    volc_voice_tech2: str = Field(
        default="",
        validation_alias=AliasChoices(
            "VOLC_VOICE_TECH2",
            "VOLC_VOICE_TYPE_SECOND_ROUND_INTERVIEWER",
        ),
    )
    volc_voice_comprehensive: str = Field(
        default="",
        validation_alias=AliasChoices(
            "VOLC_VOICE_COMPREHENSIVE",
            "VOLC_VOICE_TYPE_THIRD_ROUND_INTERVIEWER",
        ),
    )
    volc_voice_hr: str = Field(
        default="",
        validation_alias=AliasChoices(
            "VOLC_VOICE_HR",
            "VOLC_VOICE_TYPE_HR_INTERVIEWER",
        ),
    )

    # ---- 评分系统阈值 ----
    initial_score_min: int = 60
    initial_score_max: int = 80
    score_threshold_break: int = 50

    # ---- 打断阈值 ----
    max_user_answer_seconds: int = 90
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # ---- P6 / 部署模式与传输安全 ----
    # 取值 "dev" / "prod"。生产模式下 startup 强制要求 cookie_secure=True；
    # SecurityHeadersMiddleware 也只在 prod 模式追加 HSTS / CSP，避免 dev
    # 浏览器 HTTP 环境被 HSTS 永久污染本机域名。
    app_env: str = Field(
        default="dev",
        validation_alias=AliasChoices("APP_ENV", "QI_APP_ENV"),
    )
    # cookie ``Secure`` 标志位。生产 https 部署时必须 True；dev/HTTP 下 False
    # 才能让浏览器愿意带回 cookie。startup 期会做"prod + secure=False = RuntimeError"
    # 的双重把守。
    cookie_secure: bool = Field(
        default=False,
        validation_alias=AliasChoices("COOKIE_SECURE", "QI_COOKIE_SECURE"),
    )
    # 生产模式 TrustedHostMiddleware 白名单。dev 默认 ``["*"]``；prod 必须显式
    # 指定一个非 ``*`` 列表，否则 startup 抛 RuntimeError 的 fail-fast。
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["*"],
        validation_alias=AliasChoices("ALLOWED_HOSTS", "QI_ALLOWED_HOSTS"),
    )

    # ---- P6 / 邮件发送 ----
    # MailSender 工厂键。``console`` —— 仅写 backend/data/dev_mail/<...>.json，
    # 本机 + e2e 用；``smtp`` —— 真实 SMTP，需要下面一组字段全部就绪。
    mail_backend: str = Field(
        default="console",
        validation_alias=AliasChoices("MAIL_BACKEND", "QI_MAIL_BACKEND"),
    )
    smtp_host: str = Field(default="", validation_alias=AliasChoices("SMTP_HOST"))
    smtp_port: int = Field(default=587, validation_alias=AliasChoices("SMTP_PORT"))
    smtp_user: str = Field(default="", validation_alias=AliasChoices("SMTP_USER"))
    smtp_password: str = Field(default="", validation_alias=AliasChoices("SMTP_PASSWORD"))
    # 与服务商对应的传输安全档位：``starttls`` (587) / ``ssl`` (465) / ``none``。
    smtp_security: str = Field(
        default="starttls",
        validation_alias=AliasChoices("SMTP_SECURITY"),
    )
    mail_from: str = Field(default="", validation_alias=AliasChoices("MAIL_FROM"))
    # 拼接密码重置 URL 用：``{frontend_base_url}/reset-password?token=...``
    frontend_base_url: str = Field(
        default="http://127.0.0.1:5173",
        validation_alias=AliasChoices("FRONTEND_BASE_URL"),
    )
    # OTP TTL（默认 10 分钟） / 重置链接 TTL（默认 30 分钟） / 同邮箱发送
    # 节流（默认 60 秒）。
    mail_otp_ttl_minutes: int = Field(
        default=10, validation_alias=AliasChoices("MAIL_OTP_TTL_MINUTES")
    )
    mail_reset_ttl_minutes: int = Field(
        default=30, validation_alias=AliasChoices("MAIL_RESET_TTL_MINUTES")
    )
    mail_send_min_interval_sec: int = Field(
        default=60, validation_alias=AliasChoices("MAIL_SEND_MIN_INTERVAL_SEC")
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # noqa: E501
