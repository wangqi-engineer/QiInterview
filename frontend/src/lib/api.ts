import axios, { type AxiosInstance } from "axios";
import { useSettings } from "@/store/settings";

export const api: AxiosInstance = axios.create({
  baseURL: "/api",
  timeout: 60000,
  // P3 / lite-auth：所有请求都带上 ``qi_session`` cookie，后端依赖
  // ``current_user`` 注入。Vite dev server 的 ``/api`` 走 same-origin
  // 代理，浏览器认为 cookie 与前端同域，不需要额外 CORS 配置。
  withCredentials: true,
});

api.interceptors.request.use((config) => {
  const s = useSettings.getState();
  config.headers["X-LLM-Provider"] = s.llmProvider;
  if (s.llmKey) config.headers["X-LLM-Key"] = s.llmKey;
  if (s.llmModel) config.headers["X-LLM-Model"] = s.llmModel;
  if (s.dashscopeKey) config.headers["X-DashScope-Key"] = s.dashscopeKey;
  if (s.voiceAppId) config.headers["X-Voice-AppId"] = s.voiceAppId;
  if (s.voiceToken) config.headers["X-Voice-Token"] = s.voiceToken;
  if (s.voiceTtsAppId) config.headers["X-Voice-TTS-AppId"] = s.voiceTtsAppId;
  if (s.voiceTtsToken) config.headers["X-Voice-TTS-Token"] = s.voiceTtsToken;
  if (s.voiceSttAppId) config.headers["X-Voice-STT-AppId"] = s.voiceSttAppId;
  if (s.voiceSttToken) config.headers["X-Voice-STT-Token"] = s.voiceSttToken;
  if (s.voiceTtsRid) config.headers["X-Voice-TTS-Rid"] = s.voiceTtsRid;
  if (s.voiceAsrRid) config.headers["X-Voice-ASR-Rid"] = s.voiceAsrRid;
  return config;
});

export interface JobItem {
  id: number;
  source: string;
  source_post_id: string;
  title: string;
  category?: string;
  location?: string;
  department?: string;
  keyword?: string;
  responsibility?: string;
  requirement?: string;
  raw_url: string;
  fetched_at: string;
  expires_at: string;
}

export interface JobListResponse {
  items: JobItem[];
  total: number;
  page: number;
  page_size: number;
  cached: boolean;
}

export interface ImpressionDimension {
  score: number;
  reason: string;
}

export interface ImpressionBreakdown {
  status?: "pending" | "ready" | "error";
  reason?: string;
  dimensions?: Partial<Record<"education" | "experience" | "projects" | "papers" | "match", ImpressionDimension>>;
}

export interface InterviewOut {
  id: string;
  interview_type: string;
  eval_mode: string;
  llm_provider: string;
  llm_model: string;
  voice_speaker: string;
  job_id?: number | null;
  job_title: string;
  job_url?: string;
  job_jd?: string;
  resume_filename?: string;
  initial_score: number;
  final_score: number;
  end_reason?: string | null;
  created_at: string;
  ended_at?: string | null;
  impression_breakdown?: ImpressionBreakdown | null;
}

export interface TurnOut {
  id: number;
  idx: number;
  role: string;
  text: string;
  strategy?: string | null;
  expected_topic?: string | null;
  score_delta: number;
  score_after: number;
  evaluator_json?: Record<string, unknown> | null;
  started_at: string;
  ended_at?: string | null;
}

export interface InterviewDetail extends InterviewOut {
  turns: TurnOut[];
}

export interface InterviewListPage {
  items: InterviewOut[];
  total: number;
  page: number;
  page_size: number;
}

export interface ReportOut {
  session_id: string;
  summary: string;
  strengths_md: string;
  weaknesses_md: string;
  advice_md: string;
  trend: { idx: number; score: number; delta: number }[];
  turns: TurnOut[];
  created_at: string;
}

export const apiClient = {
  health: () => api.get("/health"),

  listJobs: (params: {
    source?: string;
    q?: string;
    page?: number;
    pageSize?: number;
    refresh?: boolean;
  }) => {
    // s9 配套：把 camelCase ``pageSize`` 映射成后端期待的 snake_case ``page_size``。
    const { pageSize, ...rest } = params;
    const query: Record<string, unknown> = { ...rest };
    if (pageSize !== undefined) query.page_size = pageSize;
    return api
      .get<JobListResponse>("/jobs", { params: query })
      .then((r) => r.data);
  },

  refreshJobs: () => api.post("/jobs/refresh").then((r) => r.data),

  uploadResume: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return api
      .post<{
        filename: string;
        raw_text: string;
        summary: string;
        structured: Record<string, unknown>;
      }>("/resume/upload", fd, {
        headers: { "Content-Type": "multipart/form-data" },
      })
      .then((r) => r.data);
  },

  createInterview: (
    payload: {
      interview_type: string;
      eval_mode: string;
      llm_provider: string;
      llm_model: string;
      job_id?: number;
      job_title?: string;
      job_jd?: string;
      job_url?: string;
      resume_text?: string;
      resume_filename?: string;
    },
    opts: { asyncScore?: boolean } = {},
  ) =>
    api
      .post<InterviewOut>("/interviews", payload, {
        params: opts.asyncScore ? { async_score: 1 } : undefined,
      })
      .then((r) => r.data),

  listInterviews: (
    opts: { page?: number; pageSize?: number } = {},
  ) =>
    api
      .get<InterviewListPage>("/interviews", {
        params: {
          page: opts.page ?? 1,
          page_size: opts.pageSize ?? 10,
        },
      })
      .then((r) => r.data),

  getInterview: (sid: string) =>
    api.get<InterviewDetail>(`/interviews/${sid}`).then((r) => r.data),

  deleteInterview: (sid: string) =>
    api.delete(`/interviews/${sid}`).then((r) => r.data),

  /**
   * h6 一键删除：清空所有历史面试。
   * 后端契约：``DELETE /api/interviews`` → ``{ ok, deleted }``。
   */
  deleteAllInterviews: () =>
    api
      .delete<{ ok: boolean; deleted: number }>(`/interviews`)
      .then((r) => r.data),

  endInterview: (sid: string, reason = "user") =>
    api.post(`/interviews/${sid}/end`, null, { params: { reason } }).then((r) => r.data),

  getReport: (sid: string) =>
    api.get<ReportOut>(`/reports/${sid}`).then((r) => r.data),

  regenReport: (sid: string) =>
    api.delete(`/reports/${sid}`).then((r) => r.data),

  // ── P3 / lite-auth：每用户隔离的凭据缓存 ──
  getCredentials: () =>
    api.get<UserCredentialsOut>("/credentials").then((r) => r.data),

  putCredentials: (body: Partial<UserCredentialsOut>) =>
    api.put<UserCredentialsOut>("/credentials", body).then((r) => r.data),
};

/** 与后端 ``UserCredential`` 模型一一对应；空字符串=未设置。 */
export interface UserCredentialsOut {
  llm_provider: string;
  llm_key: string;
  llm_model: string;
  llm_model_fast: string;
  llm_model_deep: string;
  /** v0.4：火山引擎语音单 API Key（TTS + STT 共用） */
  volc_voice_key: string;
  // ── 历史字段（向后兼容；前端 UI 已不暴露） ──
  dashscope_key: string;
  voice_app_id: string;
  voice_token: string;
  voice_tts_app_id: string;
  voice_tts_token: string;
  voice_stt_app_id: string;
  voice_stt_token: string;
  voice_tts_rid: string;
  voice_asr_rid: string;
}

/** 已知的报告字段，用于 SSE 流式渲染。 */
export const REPORT_FIELDS = [
  "summary",
  "strengths_md",
  "weaknesses_md",
  "advice_md",
  "score_explanation_md",
] as const;
export type ReportField = (typeof REPORT_FIELDS)[number];

export interface ReportStreamSectionDelta {
  type: "section_delta";
  section: ReportField;
  delta: string;
  closed: boolean;
}
export interface ReportStreamSectionDone {
  type: "section_done";
  section: ReportField;
}
export interface ReportStreamDone {
  type: "done";
  data: Record<ReportField, string>;
  cached: boolean;
  trend: { idx: number; score: number; delta: number }[];
}
export interface ReportStreamError {
  type: "error";
  message: string;
}
export type ReportStreamEvent =
  | ReportStreamSectionDelta
  | ReportStreamSectionDone
  | ReportStreamDone
  | ReportStreamError;

/** 用 fetch + ReadableStream 解析 SSE，自动带上凭据 header。 */
export async function* streamReport(
  sid: string,
  signal?: AbortSignal,
): AsyncGenerator<ReportStreamEvent, void, void> {
  const s = useSettings.getState();
  const headers: Record<string, string> = {
    "X-LLM-Provider": s.llmProvider,
    Accept: "text/event-stream",
  };
  if (s.llmKey) headers["X-LLM-Key"] = s.llmKey;
  if (s.llmModel) headers["X-LLM-Model"] = s.llmModel;
  if (s.dashscopeKey) headers["X-DashScope-Key"] = s.dashscopeKey;

  const resp = await fetch(`/api/reports/${sid}/stream`, {
    method: "GET",
    headers,
    signal,
    // P3 / lite-auth：reports stream 走 ``current_user`` 鉴权，必须带 cookie。
    credentials: "include",
  });
  if (!resp.ok || !resp.body) {
    throw new Error(`SSE 连接失败：${resp.status} ${resp.statusText}`);
  }
  const reader = resp.body.getReader();
  const dec = new TextDecoder("utf-8");
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const line = block
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trim())
        .join("\n");
      if (!line) continue;
      try {
        const ev = JSON.parse(line) as ReportStreamEvent;
        yield ev;
      } catch {
        // 忽略坏帧
      }
    }
  }
}
