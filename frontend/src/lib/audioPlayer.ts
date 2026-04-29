/* AI 语音流式播放：使用 MediaSource 拼接 mp3 块，达到 <1s 首音。 */

import { base64ToArrayBuffer } from "./audioCapture";

// 产品-测试可观测性契约（与 ws.ts 中保持一致）：
//   ``window.__qi_e2e`` 由 e2e instrumentation 在每次新页面开启时注入。
//   若存在则同步把音频事件时间戳推入对应数组——这样无需依赖
//   ``HTMLAudioElement.prototype.play`` / ``SourceBuffer.prototype.appendBuffer``
//   prototype hook 在 headless V8 binding 上必然成立。该路径对生产用户
//   零开销（``window.__qi_e2e`` 不存在时整段被 if 短路）。
type _QiE2EShape = {
  audioPlayEvents?: number[];
  audioBufferEvents?: number[];
  playingAtMs?: number | null;
};
function _qiE2E(): _QiE2EShape | undefined {
  return (window as unknown as { __qi_e2e?: _QiE2EShape }).__qi_e2e;
}
// #region agent log
const _AP_DBG_RUN = `ap_${Math.random().toString(36).slice(2, 8)}_${Date.now()}`;
const _apdbg = (
  location: string,
  message: string,
  data?: Record<string, unknown>
) => {
  fetch(
    "http://127.0.0.1:7756/ingest/9de27574-9d67-4459-85aa-d570f039638a",
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Debug-Session-Id": "714cc8",
      },
      body: JSON.stringify({
        sessionId: "714cc8",
        runId: _AP_DBG_RUN,
        hypothesisId: "H6-H10-WS-AUDIO",
        location,
        message,
        data,
        timestamp: Date.now(),
      }),
    }
  ).catch(() => {});
};
_apdbg("audioPlayer.ts:module_load", "module evaluated", {});
// #endregion
function _markAudioPlay(): void {
  const q = _qiE2E();
  // #region agent log
  _apdbg("audioPlayer.ts:_markAudioPlay", "playing event fired", {
    hasQ: !!q,
    hasArr: !!q && Array.isArray(q.audioPlayEvents),
    playingAtMsBefore: q?.playingAtMs ?? null,
  });
  // #endregion
  if (!q) return;
  const t = performance.now();
  if (Array.isArray(q.audioPlayEvents)) q.audioPlayEvents.push(t);
  if (q.playingAtMs == null) q.playingAtMs = t;
}
function _markAudioAppend(): void {
  const q = _qiE2E();
  // #region agent log
  _apdbg("audioPlayer.ts:_markAudioAppend", "appendBuffer about to call", {
    hasQ: !!q,
    hasArr: !!q && Array.isArray(q.audioBufferEvents),
    lenBefore: q?.audioBufferEvents?.length ?? -1,
  });
  // #endregion
  if (!q) return;
  if (Array.isArray(q.audioBufferEvents)) q.audioBufferEvents.push(performance.now());
}

export class StreamingAudioPlayer {
  private mediaSource: MediaSource | null = null;
  private sourceBuffer: SourceBuffer | null = null;
  private audio: HTMLAudioElement;
  private queue: ArrayBuffer[] = [];
  private appending = false;
  private opened = false;
  /** 当 audio.play() 被 autoplay policy 拒绝时挂载的兜底 listener；
   * 触发一次后即回收。详见 D13a 注释。 */
  private gestureRetryDispose: (() => void) | null = null;
  /** 防止同一 audio 元素挂多份 'playing' listener（reset/start 反复时）。 */
  private playingMarkerInstalled = false;

  constructor(audio: HTMLAudioElement) {
    this.audio = audio;
    this._installPlayingMarker();
  }

  /** 永久挂一个 'playing' 监听，保证 e2e 协议时间戳可见。
   *  生产环境下 ``__qi_e2e`` 不存在时仅是一个 no-op listener。 */
  private _installPlayingMarker(): void {
    if (this.playingMarkerInstalled) return;
    this.playingMarkerInstalled = true;
    this.audio.addEventListener("playing", () => {
      _markAudioPlay();
    });
  }

  async start(): Promise<void> {
    // #region agent log
    _apdbg("audioPlayer.ts:start:enter", "start() invoked", {});
    // #endregion
    this.reset();
    this.mediaSource = new MediaSource();
    this.audio.src = URL.createObjectURL(this.mediaSource);
    await new Promise<void>((resolve) => {
      this.mediaSource!.addEventListener("sourceopen", () => {
        try {
          this.sourceBuffer = this.mediaSource!.addSourceBuffer("audio/mpeg");
          this.sourceBuffer.mode = "sequence";
          this.sourceBuffer.addEventListener("updateend", () => {
            this.appending = false;
            this.flush();
          });
          this.opened = true;
          // #region agent log
          _apdbg("audioPlayer.ts:start:sourceopen_ok", "addSourceBuffer ok", {});
          // #endregion
          resolve();
        } catch (e) {
          // #region agent log
          _apdbg("audioPlayer.ts:start:sourceopen_err", "addSourceBuffer failed", {
            err: String((e as Error)?.message || e),
          });
          // #endregion
          console.error("addSourceBuffer failed", e);
          resolve();
        }
      });
    });
    // 关键修复：不要 ``await`` ``audio.play()``。
    // 在 MediaSource 模式下，play() 返回的 Promise 在「实际开始解码出声」前
    // 不会 resolve；而首音 mp3 chunk 要在 ``start()`` 返回**之后**才会通过
    // ``appendBase64``→``flush``→``appendBuffer`` 入队——形成"play 等数据 /
    // 数据等 start 返回"的死锁。表现：headless Chromium 上 ``audio.play()``
    // 永不 resolve，``InterviewPage`` 卡在 ``await playerRef.start()``，于是
    // ``appendBase64`` 永远走不到，``<audio>.playing`` 永远不触发，i5 直接
    // 超时 15 s 失败。改为 fire-and-forget：play() 在后台等数据，等
    // SourceBuffer 喂第一块 mp3 后会自然进入 playing 状态。
    void this.audio.play()
      .then(() => {
        // #region agent log
        _apdbg("audioPlayer.ts:start:play_resolved", "audio.play() resolved", {
          paused: this.audio.paused,
          readyState: this.audio.readyState,
        });
        // #endregion
      })
      .catch((e: unknown) => {
        // #region agent log
        _apdbg("audioPlayer.ts:start:play_rejected", "audio.play() rejected", {
          err: String((e as Error)?.message || e),
        });
        // #endregion
        this._installGestureRetry();
      });
  }

  /** 监听一次性 click/keydown/touchstart，把被 autoplay 拒掉的 .play() 救回来。 */
  private _installGestureRetry(): void {
    if (this.gestureRetryDispose) return; // 已挂载
    if (typeof document === "undefined") return;
    const tryPlay = () => {
      this.audio.play().catch(() => {
        // 若仍被拒（极少数浏览器策略），同样的 listener 已被 once:true 拆掉，
        // 不会循环触发；下一次 .start() 会重新挂载。
      });
      this._disposeGestureRetry();
    };
    const opts: AddEventListenerOptions = { once: true, passive: true, capture: true };
    document.addEventListener("click", tryPlay, opts);
    document.addEventListener("keydown", tryPlay, opts);
    document.addEventListener("touchstart", tryPlay, opts);
    this.gestureRetryDispose = () => {
      document.removeEventListener("click", tryPlay, opts);
      document.removeEventListener("keydown", tryPlay, opts);
      document.removeEventListener("touchstart", tryPlay, opts);
    };
  }

  private _disposeGestureRetry(): void {
    if (this.gestureRetryDispose) {
      this.gestureRetryDispose();
      this.gestureRetryDispose = null;
    }
  }

  appendBase64(b64: string): void {
    const buf = base64ToArrayBuffer(b64);
    this.queue.push(buf);
    this.flush();
  }

  private flush(): void {
    if (!this.sourceBuffer || this.appending || !this.opened) return;
    if (this.queue.length === 0) return;
    const next = this.queue.shift()!;
    try {
      this.appending = true;
      _markAudioAppend();
      this.sourceBuffer.appendBuffer(next);
    } catch (e) {
      console.warn("appendBuffer failed", e);
      this.appending = false;
    }
  }

  endOfStream(): void {
    if (this.mediaSource && this.opened && this.mediaSource.readyState === "open") {
      const tryEnd = () => {
        if (this.queue.length === 0 && !this.appending) {
          try {
            this.mediaSource!.endOfStream();
          } catch {
            /* noop */
          }
        } else {
          setTimeout(tryEnd, 80);
        }
      };
      tryEnd();
    }
  }

  /** 等 <audio> 真的把 SourceBuffer 里已缓冲的 mp3 播完。
   *
   * 之所以需要这个方法：``ai_audio_end`` 只代表后端已把所有 mp3 chunk 推完，
   * 此时前端 SourceBuffer 里还有几秒音频没播。原先 ``InterviewPage`` 收到
   * ``ai_audio_end`` 后 200 ms 就调 ``reset()`` 硬停 ``<audio>``，导致开场白/
   * next_question 这种较长文本被截断。改为：收 ``ai_audio_end`` 后调
   * ``endOfStream()`` 封口 MSE，再等本方法 resolve（``<audio>`` 自然触发
   * ``ended`` 事件或被 ``cancel()``/``pause()`` 外部打断），然后才 reset。
   *
   * 返回值：
   *   - "ended"     —— ``<audio>`` 自然播放完成（期望路径）
   *   - "cancelled" —— 被外部 ``cancel()``/``pause()`` 中止（打断场景）
   *   - "timeout"   —— 兜底超时，防止 MSE 在极端情况下永不触发 ``ended``
   */
  async waitUntilEnded(timeoutMs = 30_000): Promise<"ended" | "cancelled" | "timeout"> {
    // 已经处于 ended 状态（比如 reset 前就播完了）直接返回。
    if (!this.audio) return "ended";
    if (this.audio.ended) return "ended";
    return new Promise((resolve) => {
      let settled = false;
      const cleanup = () => {
        settled = true;
        this.audio.removeEventListener("ended", onEnded);
        this.audio.removeEventListener("pause", onPause);
        clearTimeout(timer);
      };
      const onEnded = () => {
        if (settled) return;
        // #region agent log
        _apdbg("audioPlayer.ts:waitUntilEnded:ended", "<audio> ended event", {
          currentTime: this.audio.currentTime,
          duration: this.audio.duration,
        });
        // #endregion
        cleanup();
        resolve("ended");
      };
      const onPause = () => {
        if (settled) return;
        // pause 可能是：(1) duration 到达末尾后浏览器自动 pause（此时 ended=true）；
        // (2) 外部 cancel() 调 audio.pause() 主动打断。用 ``ended`` 标志区分。
        // #region agent log
        _apdbg("audioPlayer.ts:waitUntilEnded:pause", "<audio> pause event", {
          ended: this.audio.ended,
          currentTime: this.audio.currentTime,
          duration: this.audio.duration,
        });
        // #endregion
        cleanup();
        resolve(this.audio.ended ? "ended" : "cancelled");
      };
      const timer = setTimeout(() => {
        if (settled) return;
        // #region agent log
        _apdbg("audioPlayer.ts:waitUntilEnded:timeout", "wait timed out", {
          timeoutMs,
          currentTime: this.audio.currentTime,
          duration: this.audio.duration,
          paused: this.audio.paused,
          ended: this.audio.ended,
        });
        // #endregion
        cleanup();
        resolve("timeout");
      }, timeoutMs);
      this.audio.addEventListener("ended", onEnded);
      this.audio.addEventListener("pause", onPause);
    });
  }

  /** 立即停止播放（用户打断 AI）。 */
  cancel(): void {
    try {
      this.audio.pause();
    } catch {
      /* noop */
    }
    this.queue = [];
    if (this.mediaSource && this.opened && this.mediaSource.readyState === "open") {
      try {
        this.mediaSource.endOfStream("decode");
      } catch {
        /* noop */
      }
    }
  }

  reset(): void {
    this.cancel();
    this._disposeGestureRetry();
    this.mediaSource = null;
    this.sourceBuffer = null;
    this.queue = [];
    this.appending = false;
    this.opened = false;
  }
}
