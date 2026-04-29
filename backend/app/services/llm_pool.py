"""LLM client pool.

D12 修复（VOICE-E2E-LATENCY）—— 第二阶段：
=================================================
i7（开场白首字音频）已经被 ``stream_opening`` 模板化 + ``tts_pool`` 解决。
但 i8（作答 → 首字音频）仍 ~9.7 s 超阈，根因是 LLM 端：

    {"location": "llm.chat_stream_text:enter", "after_create_ms": 7909}
    {"location": "llm.chat_stream_text:first_token", "ttft_ms": 7918}

注意 TTFT ≈ after_create_ms：第一段 SSE delta 在 ``await
client.chat.completions.create()`` 返回时几乎立刻就到了，**真正慢的是 TLS
建链 + 首包**。

每次调用都 ``build_client(creds)`` 新建一个 ``AsyncOpenAI``，底层 httpx
会拉一条新的 TLS 到 ``ark.cn-beijing.volces.com``。在当前网络环境（VPN /
MITM 代理）下，这条 TLS 大约 7-8 s。

修复策略和 ``tts_pool`` 思路一致——**把握手从用户路径上移走**：

1. 单例缓存 ``AsyncOpenAI``，按 ``(provider, api_key)`` 取；底层 httpx
   client 配置 ``keepalive_expiry=300s``，保证空闲 5 分钟内复用。
2. 启动期 ``warmup()`` 发一次 ``max_tokens=1`` 的小请求，把 TLS 连接
   预热进 httpx pool。
3. 后台每 60 s 再发一次 keepalive 小请求，避免 idle 触发 RST/FIN。

整体代码与日志埋点都参考了 ``app.services.tts_pool``。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
from openai import AsyncOpenAI

from app.core.credentials import LLMCreds
from app.services.llm import PROVIDERS


logger = logging.getLogger(__name__)


# 间隔策略：保守一些。httpx 默认 keepalive_expiry=5s 太短，我们手工拉到
# 300s（见 ``_build_async_client``），后台每隔 _KEEPALIVE_INTERVAL_S 发 N
# 路并行小请求把多条 TLS 连接全部预热进 keepalive pool。
#
# **为什么需要并行预热多条**：豆包 / 火山方舟用 HTTP/1.1（h2 未启用），单
# TLS 连接无法多路复用。当复盘生成的 deep-tier 长流（~40s）正在进行时，
# 同时进来的 fast-tier 请求只能新开一条 TLS（~7s）。预先在 pool 里塞
# ``_WARM_POOL_SIZE`` 条 idle 连接，就能在并发场景下保持冷启 ≤ 1s。
#
# 实测豆包/方舟服务端会在 ~30s idle 后单方面关闭连接（即使我们本地
# keepalive_expiry=300s），所以 ping 间隔必须 < 30s。这里 25s 给点 margin。
_KEEPALIVE_INTERVAL_S = 25.0
_WARM_POOL_SIZE = 3


# #region agent log
def _qi_pool_dbg(location: str, message: str, data: dict[str, Any]) -> None:
    """Append one NDJSON line to .cursor/debug-714cc8.log for session 714cc8."""
    try:
        path = Path(__file__).resolve().parents[3] / ".cursor" / "debug-714cc8.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "sessionId": "714cc8",
                        "runId": "be_qidbg",
                        "hypothesisId": "D12-LLM-POOL",
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
# #endregion


def _build_async_client() -> httpx.AsyncClient:
    """Configure httpx with long keepalive so subsequent calls reuse TLS."""
    limits = httpx.Limits(
        max_connections=100,
        max_keepalive_connections=20,
        keepalive_expiry=300.0,
    )
    timeout = httpx.Timeout(60.0, connect=15.0)
    return httpx.AsyncClient(limits=limits, timeout=timeout)


class _LlmClientPool:
    def __init__(self) -> None:
        self._clients: dict[tuple[str, str], AsyncOpenAI] = {}
        self._lock = asyncio.Lock()
        self._keepalive_task: asyncio.Task[Any] | None = None
        self._keep_creds: dict[tuple[str, str], LLMCreds] = {}
        self._stopped = False

    @staticmethod
    def _key(creds: LLMCreds) -> tuple[str, str]:
        return ((creds.provider or "doubao").strip(), (creds.api_key or "").strip())

    def get(self, creds: LLMCreds) -> AsyncOpenAI:
        """Return a cached AsyncOpenAI client (creating one on first call)."""
        if not creds.api_key:
            # 缺凭据：维持原 ``build_client`` 行为——上层会抛 RuntimeError。
            # 不缓存空 key 客户端，避免污染 pool。
            base_url = PROVIDERS.get(creds.provider, PROVIDERS["doubao"])
            return AsyncOpenAI(
                api_key=creds.api_key, base_url=base_url, timeout=60.0
            )
        key = self._key(creds)
        client = self._clients.get(key)
        if client is None:
            base_url = PROVIDERS.get(creds.provider, PROVIDERS["doubao"])
            http_client = _build_async_client()
            client = AsyncOpenAI(
                api_key=creds.api_key,
                base_url=base_url,
                http_client=http_client,
                timeout=60.0,
                max_retries=0,
            )
            self._clients[key] = client
            _qi_pool_dbg(
                "llm_pool.get:create",
                "instantiated cached AsyncOpenAI",
                {"provider": creds.provider, "base_url": base_url},
            )
        return client

    async def _ping_once(self, creds: LLMCreds, *, model: str) -> int | None:
        """Send a single ``max_tokens=1`` stream request and drain it.

        Returns elapsed_ms on success, ``None`` on failure.
        """
        client = self.get(creds)
        t0 = time.perf_counter()
        try:
            stream = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "1"}],
                max_tokens=1,
                temperature=0.0,
                stream=True,
            )
            # 完整 drain，确保 httpx 把连接归还 keepalive pool。
            async for _chunk in stream:
                pass
            return int((time.perf_counter() - t0) * 1000)
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            _qi_pool_dbg(
                "llm_pool.ping:err",
                "ping failed (non-fatal)",
                {
                    "provider": creds.provider,
                    "model": model,
                    "elapsed_ms": elapsed_ms,
                    "err": f"{type(exc).__name__}: {exc}",
                },
            )
            return None

    async def warmup(self, creds: LLMCreds, *, parallel: int = _WARM_POOL_SIZE) -> None:
        """Fire ``parallel`` tiny stream calls in parallel to fill keepalive pool.

        每条请求会建立一条 TLS 连接，drain 后归还 keepalive pool。N 条并行
        之后，pool 里就有 N 条空闲 warm 连接。
        """
        if not creds.api_key:
            return
        model = creds.pick_model("fast")
        t0 = time.perf_counter()
        results = await asyncio.gather(
            *[self._ping_once(creds, model=model) for _ in range(parallel)],
            return_exceptions=True,
        )
        ok = [r for r in results if isinstance(r, int)]
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        _qi_pool_dbg(
            "llm_pool.warmup:ok",
            "warmed up LLM TLS connections",
            {
                "provider": creds.provider,
                "model": model,
                "elapsed_ms": elapsed_ms,
                "parallel": parallel,
                "ok_count": len(ok),
                "ok_avg_ms": int(sum(ok) / max(1, len(ok))) if ok else None,
            },
        )

    def schedule_keepalive(self, creds: LLMCreds) -> None:
        """Make sure a background keepalive loop pings this creds periodically."""
        if not creds.api_key:
            return
        self._keep_creds[self._key(creds)] = creds
        if self._keepalive_task is None or self._keepalive_task.done():
            self._stopped = False
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(), name="llm-pool-keepalive"
            )
            _qi_pool_dbg(
                "llm_pool.keepalive:scheduled",
                "keepalive loop started",
                {
                    "interval_s": _KEEPALIVE_INTERVAL_S,
                    "providers": [k[0] for k in self._keep_creds.keys()],
                },
            )

    async def _keepalive_loop(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(_KEEPALIVE_INTERVAL_S)
                if self._stopped:
                    return
                for creds in list(self._keep_creds.values()):
                    await self.warmup(creds)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("llm_pool keepalive loop error: %s", exc)

    async def shutdown(self) -> None:
        self._stopped = True
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except Exception:
                pass
        for client in list(self._clients.values()):
            try:
                await client.close()
            except Exception:
                pass
        self._clients.clear()
        self._keep_creds.clear()


pool = _LlmClientPool()
