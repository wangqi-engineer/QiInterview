/* 麦克风采集：通过 AudioWorklet 输出 16kHz 16-bit mono PCM；并提供本地 RMS 用于 VAD。 */

// #region agent log
function _capDbg(loc: string, message: string, data: Record<string, unknown>): void {
  try {
    fetch("http://127.0.0.1:7756/ingest/9de27574-9d67-4459-85aa-d570f039638a", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Debug-Session-Id": "714cc8" },
      body: JSON.stringify({
        sessionId: "714cc8",
        runId: "d8-mic",
        hypothesisId: "D8-MIC-START",
        location: loc,
        message,
        data,
        timestamp: Date.now(),
      }),
    }).catch(() => {});
  } catch {
    /* noop */
  }
}
// #endregion

export interface AudioFrame {
  pcm: ArrayBuffer;
  rms: number;
  timestamp: number;
}

export interface AudioCaptureOptions {
  onFrame: (frame: AudioFrame) => void;
  rmsSpeakingThreshold?: number;
  rmsSilenceMs?: number;
  /** D13b：单轮连续说话最大时长（ms），用来兜底"环境噪声/fake-mic 循环回放
   * 导致 RMS 永远不掉到阈值以下、`onSpeakingEnd` 永远不触发"的情况。
   * 默认 14 000 ms（≈ 一段中等长度回答）。命中后会以正常路径触发
   * `onSpeakingEnd`，调用方逻辑无需感知。 */
  maxSpeakingMs?: number;
  onSpeakingStart?: () => void;
  onSpeakingEnd?: () => void;
}

export class AudioCapture {
  private ctx: AudioContext | null = null;
  private node: AudioWorkletNode | null = null;
  private stream: MediaStream | null = null;
  private opts: AudioCaptureOptions;

  private speaking = false;
  private silenceCount = 0;
  private silenceFrameLimit = 8;
  private rmsThreshold = 0.025;
  private maxSpeakingMs = 14_000;
  private speakingStartedAtMs = 0;

  constructor(opts: AudioCaptureOptions) {
    this.opts = opts;
    this.rmsThreshold = opts.rmsSpeakingThreshold ?? 0.025;
    this.silenceFrameLimit = Math.max(2, Math.round((opts.rmsSilenceMs ?? 800) / 100));
    this.maxSpeakingMs = Math.max(2_000, opts.maxSpeakingMs ?? 14_000);
  }

  async start(): Promise<void> {
    if (this.ctx) return;
    // #region agent log
    _capDbg("audioCapture.start:enter", "AudioCapture.start() invoked", {
      isSecureContext: typeof window !== "undefined" ? window.isSecureContext : null,
      protocol: typeof window !== "undefined" ? window.location.protocol : null,
      host: typeof window !== "undefined" ? window.location.host : null,
      hasMediaDevices: !!navigator.mediaDevices,
      hasGUM: !!navigator.mediaDevices?.getUserMedia,
      hasEnumerate: !!navigator.mediaDevices?.enumerateDevices,
    });
    try {
      const devs = await navigator.mediaDevices.enumerateDevices();
      _capDbg(
        "audioCapture.start:enumerate",
        "enumerateDevices result",
        {
          total: devs.length,
          audioInputs: devs.filter((d) => d.kind === "audioinput").length,
          firstAudioLabel: devs.find((d) => d.kind === "audioinput")?.label ?? null,
        },
      );
    } catch (err) {
      _capDbg("audioCapture.start:enumerate-error", "enumerateDevices threw", {
        msg: (err as Error)?.message ?? String(err),
      });
    }
    // #endregion
    try {
      // #region agent log
      _capDbg("audioCapture.start:before-getUserMedia", "calling getUserMedia", {});
      // #endregion
      // 注意：channelCount/sampleRate/echoCancellation 等用 `ideal` 软约束传入；
      // 精确值在 Chromium fake-device 与部分真实硬件上会触发 OverconstrainedError，
      // 而 pcm-capture.js worklet 内部已做 inputRate→16000 重采样，因此前端不必
      // 强求 getUserMedia 输出 16k —— ideal 仅作偏好提示。
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: { ideal: 1 },
          sampleRate: { ideal: 16000 },
          echoCancellation: { ideal: true },
          noiseSuppression: { ideal: true },
          autoGainControl: { ideal: true },
        },
        video: false,
      });
      // #region agent log
      _capDbg("audioCapture.start:after-getUserMedia", "getUserMedia ok", {
        tracks: this.stream.getAudioTracks().length,
      });
      // #endregion
    } catch (err: unknown) {
      const e = err as Error & { constraint?: string };
      // #region agent log
      _capDbg("audioCapture.start:error", "getUserMedia threw", {
        step: "getUserMedia",
        name: e?.name ?? null,
        msg: e?.message ?? String(err),
        constraint: e?.constraint ?? null,
        stack: (e?.stack || "").split("\n").slice(0, 4).join(" | "),
      });
      // #endregion
      throw err;
    }

    try {
      // #region agent log
      _capDbg("audioCapture.start:before-AudioContext", "constructing AudioContext", {});
      // #endregion
      this.ctx = new AudioContext();
      // #region agent log
      _capDbg("audioCapture.start:after-AudioContext", "AudioContext created", {
        sampleRate: this.ctx.sampleRate,
        state: this.ctx.state,
      });
      // #endregion
    } catch (err: unknown) {
      const e = err as Error;
      // #region agent log
      _capDbg("audioCapture.start:error", "AudioContext ctor threw", {
        step: "AudioContext",
        name: e?.name ?? null,
        msg: e?.message ?? String(err),
      });
      // #endregion
      throw err;
    }

    try {
      // #region agent log
      _capDbg("audioCapture.start:before-addModule", "audioWorklet.addModule", {
        url: "/worklets/pcm-capture.js",
      });
      // #endregion
      await this.ctx.audioWorklet.addModule("/worklets/pcm-capture.js");
      // #region agent log
      _capDbg("audioCapture.start:after-addModule", "addModule ok", {});
      // #endregion
    } catch (err: unknown) {
      const e = err as Error;
      // #region agent log
      _capDbg("audioCapture.start:error", "audioWorklet.addModule threw", {
        step: "addModule",
        name: e?.name ?? null,
        msg: e?.message ?? String(err),
      });
      // #endregion
      throw err;
    }

    try {
      const source = this.ctx.createMediaStreamSource(this.stream as MediaStream);
      // #region agent log
      _capDbg("audioCapture.start:after-createSource", "MediaStreamSource ok", {});
      // #endregion
      this.node = new AudioWorkletNode(this.ctx, "pcm-capture", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        processorOptions: { targetRate: 16000 },
      });
      // #region agent log
      _capDbg("audioCapture.start:after-newNode", "AudioWorkletNode ok", {});
      // #endregion
      this.node.port.onmessage = (e: MessageEvent) => {
        const { type, pcm, rms } = e.data ?? {};
        if (type !== "frame") return;
        this.opts.onFrame({ pcm, rms, timestamp: Date.now() });
        this._vad(rms);
      };
      source.connect(this.node);
      // 不连到 destination，避免回声
      // #region agent log
      _capDbg("audioCapture.start:done", "AudioCapture.start completed", {});
      // #endregion
    } catch (err: unknown) {
      const e = err as Error;
      // #region agent log
      _capDbg("audioCapture.start:error", "node-graph step threw", {
        step: "createSource_or_newNode",
        name: e?.name ?? null,
        msg: e?.message ?? String(err),
      });
      // #endregion
      throw err;
    }
  }

  private _vad(rms: number) {
    const now =
      typeof performance !== "undefined" ? performance.now() : Date.now();
    if (rms >= this.rmsThreshold) {
      this.silenceCount = 0;
      if (!this.speaking) {
        this.speaking = true;
        this.speakingStartedAtMs = now;
        this.opts.onSpeakingStart?.();
        return;
      }
      // D13b：连续说话超过 maxSpeakingMs 仍未见过静音，强制收尾。
      // 真实场景：用户在嘈杂环境说话、fake-mic 循环回放、RMS 始终高于阈值。
      // 不加这个兜底，本轮永不结束 → 后端拿不到 STT_final → AI 不开口 →
      // 用户报告"麦克风开着但 AI 不回应"。
      if (now - this.speakingStartedAtMs >= this.maxSpeakingMs) {
        this.speaking = false;
        this.silenceCount = 0;
        this.opts.onSpeakingEnd?.();
      }
    } else {
      if (this.speaking) {
        this.silenceCount += 1;
        if (this.silenceCount >= this.silenceFrameLimit) {
          this.speaking = false;
          this.silenceCount = 0;
          this.opts.onSpeakingEnd?.();
        }
      }
    }
  }

  async stop(): Promise<void> {
    try {
      this.node?.disconnect();
    } catch {
      /* noop */
    }
    this.node = null;
    if (this.ctx) {
      await this.ctx.close().catch(() => {});
      this.ctx = null;
    }
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
  }
}

export function arrayBufferToBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(
      null,
      Array.from(bytes.subarray(i, i + chunk)) as number[],
    );
  }
  return btoa(binary);
}

export function base64ToArrayBuffer(b64: string): ArrayBuffer {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}
