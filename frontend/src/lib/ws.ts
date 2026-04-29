/* 面试 WebSocket 客户端：协议事件 + 重连 + 凭据透传。 */

import { useSettings } from "@/store/settings";

// #region agent log
const _WS_DBG_RUN = `ws_${Math.random().toString(36).slice(2, 8)}_${Date.now()}`;
const _WS_MOD_BUILD = "WS-MOD-BUILD-PUSH-V3";
let _WS_MSG_SEEN = 0;
let _WS_AI_AUDIO_SEEN = 0;
const _wsdbg = (
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
        runId: _WS_DBG_RUN,
        hypothesisId: "H6-H10-WS-AUDIO",
        location,
        message,
        data,
        timestamp: Date.now(),
      }),
    }
  ).catch(() => {});
};
_wsdbg("ws.ts:module_load", "module evaluated", { build: _WS_MOD_BUILD });
// #endregion

export type ServerEvent =
  | { type: "ai_thinking" }
  | { type: "ai_text"; text: string; strategy?: string; expected_topic?: string }
  | { type: "ai_audio"; mime: string; chunk_b64: string; filler?: boolean }
  | { type: "ai_audio_end"; interrupted?: boolean }
  | { type: "stt_partial"; text: string }
  | { type: "stt_final"; text: string; turn_idx: number }
  | {
      type: "score_update";
      turn_idx: number;
      delta: number;
      total: number;
      evaluator: Record<string, unknown>;
    }
  | { type: "ai_interrupt"; reason: string }
  | { type: "interview_end"; reason: string }
  | { type: "error"; message: string };

export interface InterviewWSOptions {
  sid: string;
  onEvent: (ev: ServerEvent) => void;
  onOpen?: () => void;
  onClose?: () => void;
}

export class InterviewWS {
  private ws: WebSocket | null = null;
  private opts: InterviewWSOptions;
  private closed = false;

  constructor(opts: InterviewWSOptions) {
    this.opts = opts;
  }

  connect(): Promise<void> {
    const s = useSettings.getState();
    // 安全：URL 查询串中只放非敏感偏好（provider / model 名 / app_id / resource id），
    // 所有 *_key / *_token 在 onopen 后通过 {type:"auth"} 首帧发送，避免出现在浏览器
    // 历史、HTTP 访问日志、反向代理日志和 Network 面板的 Request URL 列。
    // v0.4：URL 仅放非敏感偏好（provider / model 名）。所有 *_key / *_token
    // 走 onopen 后的 auth 首帧；后端 voice_ws._SENSITIVE_QS_KEYS 会拒收
    // URL 上的 voice_key / volc_voice_key / *_token 等敏感字段。
    const params = new URLSearchParams({
      llm_provider: s.llmProvider,
      llm_model: s.llmModel,
    });
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws/interview/${this.opts.sid}?${params.toString()}`;
    this.ws = new WebSocket(url);
    return new Promise((resolve, reject) => {
      const ws = this.ws!;
      const t = setTimeout(() => reject(new Error("WS timeout")), 8000);
      ws.onopen = () => {
        clearTimeout(t);
        // #region agent log
        _wsdbg("ws.ts:onopen", "WS opened, about to send auth", {
          url: url.replace(/\?.*/, "?…"),
          hasLlmKey: !!s.llmKey,
          hasVolcVoiceKey: !!s.volcVoiceKey,
        });
        // #endregion
        try {
          // v0.4：``volc_voice_key`` 是新合同里语音通道的唯一业务字段；
          // 旧 ``dashscope_key`` / ``voice_*_token`` 仍随 auth 一起送，
          // 后端 ``voice_creds_from_query`` 已只读 ``voice_key``，多余字段
          // 会被忽略，保留它们只是为不破坏老前端版本的发送逻辑。
          ws.send(
            JSON.stringify({
              type: "auth",
              llm_provider: s.llmProvider,
              llm_model: s.llmModel,
              llm_key: s.llmKey,
              volc_voice_key: s.volcVoiceKey,
              dashscope_key: s.dashscopeKey,
              voice_app_id: s.voiceAppId,
              voice_token: s.voiceToken,
              voice_tts_app_id: s.voiceTtsAppId,
              voice_tts_token: s.voiceTtsToken,
              voice_stt_app_id: s.voiceSttAppId,
              voice_stt_token: s.voiceSttToken,
              voice_tts_rid: s.voiceTtsRid,
              voice_asr_rid: s.voiceAsrRid,
            }),
          );
        } catch (err) {
          reject(err);
          return;
        }
        this.opts.onOpen?.();
        resolve();
      };
      ws.onerror = (e) => {
        clearTimeout(t);
        reject(e);
      };
      // 注：使用 addEventListener('message', ...) 而不是 ws.onmessage = fn。
      // 历史上观察到 Chromium headless 下，对 IDL 反射属性
      // (WebSocket.prototype.onmessage) 安装 Object.defineProperty 拦截器
      // 在某些 V8 binding 路径上拿不到原始 descriptor，于是测试侧装在
      // onmessage setter 上的探针拿不到帧；改走 addEventListener 路径
      // 与现代浏览器对齐，语义等价于单一 message listener。
      ws.addEventListener("message", (e: MessageEvent) => {
        try {
          // 产品侧可观测性钩子：
          // 当页面被 e2e instrumentation 注入了 window.__qi_e2e
          // (VOICE_LATENCY_INIT_SCRIPT 同名契约)，且消息是 ai_audio 帧时，
          // 直接 push 一条 performance.now() 时间戳到 aiAudioMsgEvents。
          // 这是 *测试-产品 协议*：测试脚本本身不变，产品只是守护
          // “只要 ai_audio 帧到达浏览器就有可观测时间戳”这一不变量，
          // 即便 WebSocket.prototype hook 在 headless V8 binding 下失效。
          // 对生产环境无副作用：window.__qi_e2e 只在 e2e 注入时存在，
          // 普通用户访问时 typeof === "undefined"，整段被 if 短路。
          if (typeof e.data === "string") {
            // ── 测试-产品协议（i9/i10/i11 扩展）──
            // 头戴 Chromium headless 下 WebSocket.prototype.addEventListener 拦截
            // 不稳定，所以由产品侧把"已经到达浏览器"的 i9/i10/i11 关注帧一并
            // 推到 window.__qi_e2e 上。仅在 e2e instrumentation 注入时存在；
            // 普通用户访问时 typeof === 'undefined'，下方 if 整体短路，零副作用。
            const w = window as unknown as {
              __qi_e2e?: {
                aiAudioMsgEvents?: number[];
                aiAudioMsgEventsFull?: { t: number; filler: boolean }[];
                aiAudioEndEvents?: { t: number; interrupted: boolean }[];
                aiTextEvents?: { t: number; text: string }[];
                sttPartialEvents?: { t: number; text: string }[];
                sttFinalEvents?: { t: number; text: string }[];
              };
            };
            const q = w.__qi_e2e;
            let _parsed: Record<string, unknown> | undefined;
            try {
              _parsed = JSON.parse(e.data);
            } catch {
              /* noop */
            }
            const _parsedType =
              typeof _parsed?.type === "string"
                ? (_parsed.type as string)
                : undefined;
            const _now = performance.now();
            const isAiAudio = _parsedType === "ai_audio";
            if (isAiAudio) {
              // #region agent log
              _wsdbg("ws.ts:onmessage:push_check", "evaluating push", {
                hasQ: !!q,
                hasArr: !!q && Array.isArray(q.aiAudioMsgEvents),
                lenBefore: q?.aiAudioMsgEvents?.length ?? -1,
                head: e.data.slice(0, 80),
              });
              // #endregion
              if (q && Array.isArray(q.aiAudioMsgEvents)) {
                q.aiAudioMsgEvents.push(_now);
                // #region agent log
                _wsdbg("ws.ts:onmessage:pushed", "PUSHED to aiAudioMsgEvents", {
                  lenAfter: q.aiAudioMsgEvents.length,
                });
                // #endregion
              }
              if (q && Array.isArray(q.aiAudioMsgEventsFull)) {
                q.aiAudioMsgEventsFull.push({
                  t: _now,
                  filler: !!_parsed?.filler,
                });
              }
            } else if (_parsedType === "ai_audio_end") {
              if (q && Array.isArray(q.aiAudioEndEvents)) {
                q.aiAudioEndEvents.push({
                  t: _now,
                  interrupted: !!_parsed?.interrupted,
                });
              }
            } else if (_parsedType === "ai_text") {
              if (q && Array.isArray(q.aiTextEvents)) {
                q.aiTextEvents.push({
                  t: _now,
                  text: String(_parsed?.text ?? ""),
                });
              }
            } else if (_parsedType === "stt_partial") {
              if (q && Array.isArray(q.sttPartialEvents)) {
                q.sttPartialEvents.push({
                  t: _now,
                  text: String(_parsed?.text ?? ""),
                });
              }
            } else if (_parsedType === "stt_final") {
              if (q && Array.isArray(q.sttFinalEvents)) {
                q.sttFinalEvents.push({
                  t: _now,
                  text: String(_parsed?.text ?? ""),
                });
              }
            }
          }
          // #region agent log
          _WS_MSG_SEEN += 1;
          const _t =
            typeof e.data === "string"
              ? (() => {
                  try {
                    return JSON.parse(e.data).type || "?";
                  } catch {
                    return "?";
                  }
                })()
              : `binary(${(e.data as ArrayBuffer)?.byteLength || 0})`;
          if (_WS_MSG_SEEN <= 5) {
            const w = window as unknown as {
              __qi_e2e?: { aiAudioMsgEvents?: unknown[] };
              __qi_e2e_installed?: boolean;
            };
            _wsdbg("ws.ts:onmessage", "WS frame received", {
              n: _WS_MSG_SEEN,
              type: _t,
              isString: typeof e.data === "string",
              len: typeof e.data === "string" ? e.data.length : undefined,
              e2eInstalled: !!w.__qi_e2e_installed,
              e2eAiAudioLen: w.__qi_e2e?.aiAudioMsgEvents?.length ?? -1,
            });
          }
          if (_t === "ai_audio") {
            _WS_AI_AUDIO_SEEN += 1;
            if (_WS_AI_AUDIO_SEEN <= 3) {
              const w = window as unknown as {
                __qi_e2e?: { aiAudioMsgEvents?: unknown[] };
              };
              _wsdbg("ws.ts:onmessage:ai_audio", "first ai_audio frame", {
                n: _WS_AI_AUDIO_SEEN,
                msgIdx: _WS_MSG_SEEN,
                e2eAiAudioLen: w.__qi_e2e?.aiAudioMsgEvents?.length ?? -1,
              });
            }
          }
          // #endregion
          const data = JSON.parse(e.data) as ServerEvent;
          this.opts.onEvent(data);
        } catch {
          /* noop */
        }
      });
      // #region agent log
      const _origOnError = ws.onerror;
      ws.onerror = (e) => {
        _wsdbg("ws.ts:onerror", "WS error event", {
          msg: String((e as ErrorEvent)?.message || ""),
        });
        if (_origOnError) (_origOnError as (ev: Event) => void).call(ws, e);
      };
      // #endregion
      ws.onclose = (ev) => {
        // #region agent log
        _wsdbg("ws.ts:onclose", "WS closed", {
          code: (ev as CloseEvent)?.code,
          reason: (ev as CloseEvent)?.reason,
          wasClean: (ev as CloseEvent)?.wasClean,
          totalMsgs: _WS_MSG_SEEN,
          totalAiAudio: _WS_AI_AUDIO_SEEN,
        });
        // #endregion
        if (!this.closed) this.opts.onClose?.();
      };
    });
  }

  send(msg: Record<string, unknown>): void {
    const _rs = this.ws?.readyState;
    const _t = (msg as { type?: string })?.type;
    // #region agent log
    _wsdbg("ws.ts:send", "send() invoked", {
      type: _t,
      readyState: _rs,
      isOpen: _rs === WebSocket.OPEN,
      hasWs: !!this.ws,
    });
    // #endregion
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    } else {
      // #region agent log
      _wsdbg("ws.ts:send:dropped", "send() DROPPED (ws not OPEN)", {
        type: _t,
        readyState: _rs,
      });
      // #endregion
    }
  }

  start(): void {
    this.send({ type: "start" });
  }

  sendAudioFrame(pcmB64: string): void {
    this.send({ type: "audio_chunk", pcm_base64: pcmB64 });
  }

  endTurn(fallback?: string): void {
    this.send({ type: "end_turn", fallback_text: fallback || "" });
  }

  sendAnswerText(text: string): void {
    this.send({ type: "answer_text", text });
  }

  interrupt(): void {
    this.send({ type: "user_interrupt" });
  }

  // i12 / i14 — 语音手动化（v0.2）：用户点 [朗读] 按钮，把 AI 文本气泡里
  // 的整段内容一次性发回后端 TTS。若上一段还在播，后端会先 cancel_tts。
  requestTTS(text: string): void {
    this.send({ type: "client_replay_tts", text });
  }

  endInterview(): void {
    this.send({ type: "end_interview" });
  }

  close(): void {
    this.closed = true;
    try {
      this.ws?.close();
    } catch {
      /* noop */
    }
    this.ws = null;
  }
}
