"""TTS 连接池 — v0.4 起退化为永远 miss 的 no-op。

历史背景：旧的 TTS（火山 V3 双向流式 / 阿里 CosyVoice）建立 WS 长连接需
要 5–10 s 的 TLS+WS 握手，所以服务器端有一个 warm pool 在 lifespan
startup 时提前拨好若干条 session 备用。

v0.4 切到火山 ``api/v3/tts/unidirectional``（HTTP POST）以后：
  1. 没有"长连接"概念，每次 ``TtsSession`` 复用同一个 ``httpx.AsyncClient``，
     连接复用本身已经是 sub-second；
  2. 每条 TTS 还**强制需要前端透传的** ``volc_voice_key``，服务端启动时根
     本无法预知首位用户会用哪把 key（业务侧严禁回退环境变量），warm 起来
     的 session 也用不上。

为保不破坏旧调用点，``pool.acquire`` 这些方法依旧存在，但行为简化为：
  - ``acquire`` → 永远返回 ``None``（让调用方走 lazy connect 分支）；
  - ``configure`` / ``warmup_keys`` / ``shutdown`` → 完全 no-op；
  - ``is_enabled`` → 永远 ``False``。

调用方（``voice_ws.play_text_stream`` / ``voice_ws.warmup``）不需要改动；
它们已经在 acquire 返回 None 时 fall back 到 ``TtsSession()`` 直建。"""
from __future__ import annotations

import logging

from app.core.credentials import VoiceCreds


logger = logging.getLogger(__name__)


class _TtsWarmPool:
    """v0.4 起为兼容性占位类。不再持有任何 session 状态。"""

    def configure(self, creds: VoiceCreds) -> None:
        return None

    def is_enabled(self) -> bool:
        return False

    def warmup_keys(self, keys: list[tuple[str, int, str]]) -> None:
        return None

    async def acquire(
        self,
        creds: VoiceCreds,
        speaker: str,
        *,
        sample_rate: int = 24000,
        audio_format: str = "mp3",
    ):
        return None

    async def shutdown(self) -> None:
        return None


pool = _TtsWarmPool()
