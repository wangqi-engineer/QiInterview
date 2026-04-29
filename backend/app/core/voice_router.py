"""根据面试类型挑选音色（火山引擎 OpenSpeech BigTTS 音色 ID）。

设计：
  - 代码内 ``VOICE_MAP`` 提供四类面试官的默认音色（fallback）；
  - **可被 ``.env.local`` 覆盖**：``VOLC_VOICE_TECH1`` / ``VOLC_VOICE_TECH2``
    / ``VOLC_VOICE_COMPREHENSIVE`` / ``VOLC_VOICE_HR``；
  - 任意时刻 ``pick_speaker(interview_type)`` 优先走 settings 的覆盖值，
    没填回退到 ``VOICE_MAP``，再没命中走 ``DEFAULT_SPEAKER``。

为什么这部分留在 settings 而不是前端？—— 这是"服务侧偏好"，与 *计费 /
账户 / 用户身份* 无关。运维只需改 ``.env.local`` 即可调整面试官人设，
不需要每个用户在 UI 重新选一遍。

| 面试类型     | 含义                 | 默认音色                               |
|--------------|----------------------|---------------------------------------|
| hr           | HR 面：温和女声       | ``zh_female_qingxin_bigtts``          |
| tech1        | 技术一面：沉稳男声    | ``zh_male_M392_conversation_wvae_bigtts`` |
| tech2        | 技术二面：清晰男声    | ``zh_male_xiaoming_conversation_wvae_bigtts`` |
| comprehensive| 综合面：知性女声      | ``zh_female_jingjing_bigtts``         |

具体音色 ID 以火山控制台『豆包语音合成』开通列表为准。tech1 / tech2 在
默认值上**已经分开**——之前都用同一条 ID，会被新增的 e2e ``test_v22``
"两两不同"断言抓住。
"""
from __future__ import annotations

import os

from app.config import get_settings


VOICE_MAP: dict[str, str] = {
    "hr": "zh_female_qingxin_bigtts",
    "tech1": "zh_male_M392_conversation_wvae_bigtts",
    "tech2": "zh_male_xiaoming_conversation_wvae_bigtts",
    "comprehensive": "zh_female_jingjing_bigtts",
}

DEFAULT_SPEAKER = (
    os.environ.get("VOLC_TTS_VOICE", "").strip()
    or "zh_male_M392_conversation_wvae_bigtts"
)


def pick_speaker(interview_type: str) -> str:
    """按面试类型 → 音色 ID。覆盖优先级：env (settings) > VOICE_MAP > DEFAULT。"""
    s = get_settings()
    override = {
        "tech1": s.volc_voice_tech1,
        "tech2": s.volc_voice_tech2,
        "comprehensive": s.volc_voice_comprehensive,
        "hr": s.volc_voice_hr,
    }.get(interview_type, "")
    override = (override or "").strip()
    if override:
        return override
    return VOICE_MAP.get(interview_type, DEFAULT_SPEAKER)
