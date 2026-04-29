import { create } from "zustand";
import { persist } from "zustand/middleware";

export type LLMProvider = "doubao" | "deepseek" | "qwen" | "glm";
export type InterviewType = "tech1" | "tech2" | "comprehensive" | "hr";
export type EvalMode = "realtime" | "summary";

export interface SettingsState {
  llmProvider: LLMProvider;
  llmKey: string;
  llmModel: string;
  /** 火山引擎语音单 API Key（``api/v3/tts/unidirectional`` + ``sauc/bigmodel_async`` 共用） */
  volcVoiceKey: string;
  // ---- 历史字段（向后兼容；新业务流不再使用，保留是为了不丢用户旧数据） ----
  /** 阿里云百炼 API Key（CosyVoice + Paraformer 共用，sk-... 前缀，已退役） */
  dashscopeKey: string;
  voiceAppId: string;
  voiceToken: string;
  voiceTtsAppId: string;
  voiceTtsToken: string;
  voiceSttAppId: string;
  voiceSttToken: string;
  voiceTtsRid: string;
  voiceAsrRid: string;
  setSettings: (partial: Partial<SettingsState>) => void;
}

// 用户痛点 s11：之前为每个 provider 预选了一个具体型号，对刚拿到 API Key 的
// 用户而言这相当于"我以为你想用 doubao-seed-1-6-251015"，实际使用方往往
// 因控制台 quota / region 不一致而踩坑。改为留空 → 让用户主动填，避免误判。
//
// 仍保留 DEFAULT_MODELS 作为推荐参考（占位符 placeholder 时使用），
// 但不再写入 store 默认值。
const DEFAULT_MODELS: Record<LLMProvider, string> = {
  doubao: "doubao-seed-1-6-251015",
  deepseek: "",
  qwen: "",
  glm: "",
};

// 持有 API Key / Access Token 的字段。绝不写入 localStorage —— 仅存活
// 在 zustand 内存 store 中，标签关闭即随之消失。
//
// 安全契约：
//   1. 这些字段不会出现在 localStorage / sessionStorage / IndexedDB；
//   2. 历史版本（v1）曾把它们明文持久化，下方 migrate 会在 v1 → v2 时
//      原地剔除；
//   3. partialize 是兜底闸门：即便有人误增字段，也不会泄漏到磁盘。
const SENSITIVE_KEYS = [
  "llmKey",
  // v0.4：火山语音 API Key 与 LLM Key 同等敏感；只在内存里、永不落盘。
  "volcVoiceKey",
  "dashscopeKey",
  "voiceToken",
  "voiceTtsToken",
  "voiceSttToken",
] as const;

function stripSensitive<T extends Record<string, unknown>>(s: T): T {
  const out = { ...s } as Record<string, unknown>;
  for (const k of SENSITIVE_KEYS) {
    if (k in out) delete out[k];
  }
  return out as T;
}

export const useSettings = create<SettingsState>()(
  persist(
    (set) => ({
      llmProvider: "doubao",
      llmKey: "",
      // s17：火山引擎是默认 provider，按用户合同模型应自动填 doubao-seed-2-0-pro-260215；
      // 其它 provider 再切换时会被 SetupPage 的 onValueChange 清空。
      llmModel: "doubao-seed-2-0-pro-260215",
      volcVoiceKey: "",
      dashscopeKey: "",
      voiceAppId: "",
      voiceToken: "",
      voiceTtsAppId: "",
      voiceTtsToken: "",
      voiceSttAppId: "",
      voiceSttToken: "",
      voiceTtsRid: "volc.service_type.10029",
      voiceAsrRid: "volc.bigasr.sauc.duration",
      setSettings: (p) => set((s) => ({ ...s, ...p })),
    }),
    {
      name: "qiinterview-settings",
      version: 2,
      partialize: (state) =>
        stripSensitive(state as unknown as Record<string, unknown>) as unknown as SettingsState,
      migrate: (persisted, _version) => {
        // v1 → v2：v1 把 llmKey / dashscopeKey / voice*Token 明文写到了
        // localStorage（CVE 等级：本地用户 / 浏览器扩展 / XSS 都能直读）。
        // v2 起这些字段只存在内存里。下面这步把任何残留密钥从历史 payload
        // 中剥掉，避免老用户继续把密钥带进新版本。
        if (persisted && typeof persisted === "object") {
          return stripSensitive(
            persisted as Record<string, unknown>,
          ) as unknown as SettingsState;
        }
        return persisted as SettingsState;
      },
      onRehydrateStorage: () => () => {
        // 兜底：在 hydrate 完成那一刻，把磁盘上的旧 payload 用 partialize
        // 后的副本立刻覆盖，关闭"migrate 完成 → zustand 下次写盘"之间的窗口。
        if (typeof window === "undefined") return;
        try {
          const raw = window.localStorage.getItem("qiinterview-settings");
          if (!raw) return;
          const parsed = JSON.parse(raw) as { state?: unknown; version?: number };
          if (parsed && parsed.state && typeof parsed.state === "object") {
            const cleaned = stripSensitive(
              parsed.state as Record<string, unknown>,
            );
            window.localStorage.setItem(
              "qiinterview-settings",
              JSON.stringify({ state: cleaned, version: 2 }),
            );
          }
        } catch {
          /* 解析失败也无妨：partialize 会兜住下一次写盘 */
        }
      },
    },
  ),
);

// E2E hook（仅 DEV mode 暴露）：测试代码可以通过
// ``window.__qiSettings.getState().setSettings({...})`` 在不点击 UI 的情况下
// 把 ``volcVoiceKey`` / ``llmKey`` 等敏感字段塞进 zustand 内存态，
// 用来支撑"复用 session、新页面打开 /interview"这类不再走 SetupPage 的
// e2e 用例。生产构建（``import.meta.env.PROD``）下不挂，避免任何 XSS 副作用。
if (typeof window !== "undefined" && import.meta.env.DEV) {
  (window as unknown as { __qiSettings?: typeof useSettings }).__qiSettings =
    useSettings;
}

export const PROVIDER_DEFAULT_MODELS = DEFAULT_MODELS;
