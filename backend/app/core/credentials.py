"""从前端请求 header / WebSocket auth 首帧透传 LLM / 语音凭据。

QiInterview v0.4 / 火山语音重构：
  - **业务凭据唯一来源 = 前端透传**。这意味着 LLM API Key、LLM
    provider/model、火山语音 API Key 等 *所有* 与计费 / 走哪个账户相关的
    字段，都不再从 ``app.config.Settings`` 回退；前端没传就直接走"无凭据"
    分支（业务侧自然 401 / 降级 / 报错）。
  - ``.env.local`` 中保留的 ``ARK_API_KEY`` / ``LLM_*`` / ``VOLC_VOICE_KEY``
    等只剩两类合法用途：
      (1) e2e 脚本读出来再 *模拟* 用户在 UI 输入，触发产品本身的"前端透传"
          路径；
      (2) 服务侧偏好（如 ``VOLC_VOICE_TECH1`` 音色路由），见
          [voice_router.py](backend/app/core/voice_router.py)。
  - 历史的双头鉴权字段（``app_id`` / ``access_token`` / ``tts_app_id`` ...）
    全部废弃；新版 ``services/tts.py`` 与 ``services/stt.py`` 只读
    ``volc_voice_key``。
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header


@dataclass
class LLMCreds:
    provider: str
    api_key: str
    model: str
    # 档位拆分：fast 用于开场白/出题/评分/印象分/打断/收尾；deep 用于复盘报告。
    # 留空则回退到 ``model``。
    model_fast: str = ""
    model_deep: str = ""

    def pick_model(self, tier: str = "fast") -> str:
        if tier == "deep":
            return (self.model_deep or self.model).strip() or self.model
        if tier == "fast":
            return (self.model_fast or self.model).strip() or self.model
        return self.model


@dataclass
class VoiceCreds:
    """语音凭据。

    新版火山引擎接口（``api/v3/tts/unidirectional`` 与
    ``api/v3/sauc/bigmodel_async``）只需要单一 ``X-Api-Key``；同一把 key
    既能驱动 TTS 也能驱动 STT。``volc_voice_key`` 因此是这个数据类的
    唯一**业务**字段。

    其它历史字段（``app_id`` / ``access_token`` / ``dashscope_api_key`` ...）
    保留了**字段名**但不再参与任何判定 —— 留空即可。它们存在的唯一原因
    是：旧版 e2e 脚本 / DB schema 还在引用这些名字，删除会引发不必要的
    回归。新代码请只读 ``volc_voice_key``。
    """

    # ---- 主业务字段（唯一来源 = 前端 auth 首帧）----
    volc_voice_key: str = ""

    # ---- 历史字段（保留兼容；新版语音模块不读）----
    app_id: str = ""
    access_token: str = ""
    tts_app_id: str = ""
    tts_access_token: str = ""
    stt_app_id: str = ""
    stt_access_token: str = ""
    tts_resource_id: str = ""
    asr_resource_id: str = ""
    dashscope_api_key: str = ""

    def voice_key_effective(self) -> str:
        """返回当前可用的 ``X-Api-Key``（已 strip）。空字符串 = 无凭据。"""
        return (self.volc_voice_key or "").strip()

    def has_voice_creds(self) -> bool:
        """新合同：只要前端透传了非空 ``volc_voice_key``，语音通道就视为可用。"""
        return bool(self.voice_key_effective())


def llm_credentials(
    x_llm_provider: str | None = Header(default=None, alias="X-LLM-Provider"),
    x_llm_key: str | None = Header(default=None, alias="X-LLM-Key"),
    x_llm_model: str | None = Header(default=None, alias="X-LLM-Model"),
    x_llm_model_fast: str | None = Header(default=None, alias="X-LLM-Model-Fast"),
    x_llm_model_deep: str | None = Header(default=None, alias="X-LLM-Model-Deep"),
) -> LLMCreds:
    """REST 路径上的 LLM 凭据。

    安全约束（**不**回退到 env）：
      - ``api_key`` —— 谁付费就该谁说了算，必须前端显式提供。
      - ``provider`` / ``model`` —— 与账户 / 上下文绑定，同上。

    可回退到 env 的"配置项"（不是凭据）：
      - ``model_fast`` / ``model_deep`` —— 这两条只是档位选择，前端目前
        没有 UI 暴露（设计上由部署侧决定哪一档跑哪个 model），
        因此从 ``Settings.llm_model_fast`` / ``llm_model_deep`` 兜底。
        如果 env 里也没设，``LLMCreds.pick_model`` 自然回落到 ``model``。
    """
    from app.config import get_settings

    s = get_settings()
    return LLMCreds(
        provider=(x_llm_provider or "").strip(),
        api_key=(x_llm_key or "").strip(),
        model=(x_llm_model or "").strip(),
        model_fast=((x_llm_model_fast or "").strip() or (s.llm_model_fast or "").strip()),
        model_deep=((x_llm_model_deep or "").strip() or (s.llm_model_deep or "").strip()),
    )


def voice_credentials(
    x_volc_voice_key: str | None = Header(default=None, alias="X-Volc-Voice-Key"),
) -> VoiceCreds:
    """REST 路径上的语音凭据。新版只接受 ``X-Volc-Voice-Key``；历史 header
    （``X-DashScope-Key`` / ``X-Voice-AppId`` 等）已退役，本函数不再消费。"""
    return VoiceCreds(volc_voice_key=(x_volc_voice_key or "").strip())


def voice_creds_from_query(
    voice_key: str = "",
    # ---- 旧字段：仅作签名兼容，0 业务作用 ----
    dashscope_key: str = "",
    app_id: str = "",
    token: str = "",
    tts_app_id: str = "",
    tts_token: str = "",
    stt_app_id: str = "",
    stt_token: str = "",
    tts_rid: str = "",
    asr_rid: str = "",
) -> VoiceCreds:
    """WebSocket 鉴权专用：``voice_key`` 来自 ``auth`` 首帧 ``volc_voice_key``。

    其余形参留下来只为**调用点不动**就能从老协议过渡到新协议；它们的值
    一律被忽略——业务侧只看 ``voice_key``。
    """
    del dashscope_key, app_id, token, tts_app_id, tts_token
    del stt_app_id, stt_token, tts_rid, asr_rid
    return VoiceCreds(volc_voice_key=(voice_key or "").strip())


def llm_creds_from_query(
    provider: str = "",
    key: str = "",
    model: str = "",
    model_fast: str = "",
    model_deep: str = "",
) -> LLMCreds:
    """WebSocket 鉴权专用：与 REST 版同样的回退策略。

    ``api_key`` / ``provider`` / ``model`` 不回退 env；只有 ``model_fast``
    / ``model_deep`` 这两条档位配置允许从 ``Settings`` 兜底（前端目前没
    UI，让部署侧 ``LLM_MODEL_FAST`` / ``LLM_MODEL_DEEP`` 决定）。"""
    from app.config import get_settings

    s = get_settings()
    return LLMCreds(
        provider=(provider or "").strip(),
        api_key=(key or "").strip(),
        model=(model or "").strip(),
        model_fast=((model_fast or "").strip() or (s.llm_model_fast or "").strip()),
        model_deep=((model_deep or "").strip() or (s.llm_model_deep or "").strip()),
    )
