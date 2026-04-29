import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Badge } from "@/components/ui/badge";
import { JobPicker } from "@/components/JobPicker";
import { apiClient, type JobItem } from "@/lib/api";
import {
  PROVIDER_DEFAULT_MODELS,
  useSettings,
  type EvalMode,
  type InterviewType,
  type LLMProvider,
} from "@/store/settings";
import { useAuth } from "@/store/auth";

/** s14：简历上传文件大小上限。后端 ``MAX_RESUME_BYTES`` 与该值同步。 */
const RESUME_MAX_MB = 5;
import { FileUp, Loader2, Mic, PlayCircle } from "lucide-react";

const INTERVIEW_TYPES: { value: InterviewType; label: string; desc: string }[] = [
  { value: "tech1", label: "技术一面", desc: "知识广度，覆盖基础与原理" },
  { value: "tech2", label: "技术二面", desc: "项目经验深挖，逐层下钻" },
  { value: "comprehensive", label: "综合面", desc: "技术 + 系统设计 + 软素质" },
  { value: "hr", label: "HR 面", desc: "职业规划、价值观、薪酬期望" },
];

export default function SetupPage() {
  const nav = useNavigate();
  const settings = useSettings();
  const me = useAuth((s) => s.me);
  const [interviewType, setInterviewType] = useState<InterviewType>("tech1");
  const [evalMode, setEvalMode] = useState<EvalMode>("realtime");
  const [job, setJob] = useState<JobItem | null>(null);
  const [manualJobTitle, setManualJobTitle] = useState("");
  const [manualJobJD, setManualJobJD] = useState("");
  const [resumeText, setResumeText] = useState("");
  const [resumeFilename, setResumeFilename] = useState("");
  const [uploading, setUploading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");

  // P3 / a3：用户切换时把内存中的 settings 全清掉，再从后端拉自己的凭据。
  // 避免 alice 退出后 bob 在前端还能看到 alice 的 llmKey（即便后端隔离已生效）。
  const lastUserRef = useRef<number | null>(null);
  const setSettings = useSettings((s) => s.setSettings);
  useEffect(() => {
    const myId = me?.id ?? null;
    if (lastUserRef.current === myId) return;
    lastUserRef.current = myId;
    if (!myId) {
      setSettings({
        llmKey: "",
        llmModel: "",
        volcVoiceKey: "",
        dashscopeKey: "",
        voiceAppId: "",
        voiceToken: "",
        voiceTtsAppId: "",
        voiceTtsToken: "",
        voiceSttAppId: "",
        voiceSttToken: "",
      });
      return;
    }
    // 先把内存中的 key 清空，避免上一个用户残留可见
    setSettings({
      llmKey: "",
      volcVoiceKey: "",
      dashscopeKey: "",
      voiceToken: "",
      voiceTtsToken: "",
      voiceSttToken: "",
    });
    void apiClient
      .getCredentials()
      .then((c) => {
        setSettings({
          llmProvider: (c.llm_provider as LLMProvider) || "doubao",
          llmKey: c.llm_key || "",
          llmModel:
            c.llm_model || (c.llm_provider === "doubao" ? "doubao-seed-2-0-pro-260215" : ""),
          volcVoiceKey: c.volc_voice_key || "",
          dashscopeKey: c.dashscope_key || "",
          voiceAppId: c.voice_app_id || "",
          voiceToken: c.voice_token || "",
          voiceTtsAppId: c.voice_tts_app_id || "",
          voiceTtsToken: c.voice_tts_token || "",
          voiceSttAppId: c.voice_stt_app_id || "",
          voiceSttToken: c.voice_stt_token || "",
          voiceTtsRid: c.voice_tts_rid || "volc.service_type.10029",
          voiceAsrRid: c.voice_asr_rid || "volc.bigasr.sauc.duration",
        });
      })
      .catch(() => {
        // 拉失败不阻塞 UI；下一次写凭据时再 upsert。
      });
  }, [me?.id, setSettings]);

  // 简单 debounce-style 持久化：用户每次改 LLM/语音相关字段，500 ms 静默后
  // 同步到后端，让其他设备 / 重登也能取回（对应 a3）。
  useEffect(() => {
    if (!me?.id) return;
    const handle = setTimeout(() => {
      void apiClient
        .putCredentials({
          llm_provider: settings.llmProvider,
          llm_key: settings.llmKey,
          llm_model: settings.llmModel,
          volc_voice_key: settings.volcVoiceKey,
          dashscope_key: settings.dashscopeKey,
          voice_app_id: settings.voiceAppId,
          voice_token: settings.voiceToken,
          voice_tts_app_id: settings.voiceTtsAppId,
          voice_tts_token: settings.voiceTtsToken,
          voice_stt_app_id: settings.voiceSttAppId,
          voice_stt_token: settings.voiceSttToken,
          voice_tts_rid: settings.voiceTtsRid,
          voice_asr_rid: settings.voiceAsrRid,
        })
        .catch(() => {
          // 静默失败：避免在网络抖动时打扰用户；下次再写时会重试。
        });
    }, 500);
    return () => clearTimeout(handle);
  }, [
    me?.id,
    settings.llmProvider,
    settings.llmKey,
    settings.llmModel,
    settings.volcVoiceKey,
    settings.dashscopeKey,
    settings.voiceAppId,
    settings.voiceToken,
    settings.voiceTtsAppId,
    settings.voiceTtsToken,
    settings.voiceSttAppId,
    settings.voiceSttToken,
    settings.voiceTtsRid,
    settings.voiceAsrRid,
  ]);

  const onUploadResume = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    // s14：前端先于网络请求拦一刀，避免用户上传 100 MB+ 文件后才在后端踢回。
    if (file.size > RESUME_MAX_MB * 1024 * 1024) {
      setError(
        `简历文件 ${(file.size / 1024 / 1024).toFixed(1)} MB 超过 ${RESUME_MAX_MB} MB 上限，` +
          "请压缩或精简后再上传。",
      );
      e.target.value = "";
      return;
    }
    setUploading(true);
    setError("");
    try {
      const resp = await apiClient.uploadResume(file);
      setResumeText(resp.summary || resp.raw_text);
      setResumeFilename(resp.filename);
    } catch (err: any) {
      setError("简历解析失败：" + (err?.response?.data?.detail || err.message));
    } finally {
      setUploading(false);
    }
  };

  const startInterview = async () => {
    setError("");
    if (!settings.llmKey) {
      setError("请先填写 LLM API Key（左侧『凭据配置』）");
      return;
    }
    // 语音凭据可选：未填则后端从 .env.local 兜底，仍未配置时前端用浏览器原生 SpeechSynthesis 朗读。
    const job_title = job?.title || manualJobTitle;
    if (!job_title) {
      setError("请选择一个岗位，或手动填写岗位名称");
      return;
    }
    setCreating(true);
    try {
      // 走异步印象分路径：POST 立刻返回 sid，进入面试页时印象分仍可能在算
      const created = await apiClient.createInterview(
        {
          interview_type: interviewType,
          eval_mode: evalMode,
          llm_provider: settings.llmProvider,
          llm_model: settings.llmModel,
          job_id: job?.id,
          job_title,
          job_jd: job?.requirement
            ? `${job.responsibility || ""}\n${job.requirement}`
            : manualJobJD,
          job_url: job?.raw_url,
          resume_text: resumeText,
          resume_filename: resumeFilename,
        },
        { asyncScore: true },
      );
      nav(`/interview/${created.id}`);
    } catch (err: any) {
      setError("创建失败：" + (err?.response?.data?.detail || err.message));
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="grid lg:grid-cols-[420px_1fr] gap-6">
      {/* 左：凭据 + 模式 */}
      <div className="space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>凭据配置</CardTitle>
            <CardDescription>
              API Key 仅保留在当前标签页的内存中，关闭页面后需重新输入；不写入
              localStorage / cookies。所有调用通过后端透传，不上传第三方。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-2 gap-2">
              <div className="space-y-1.5">
                <Label>LLM 服务商</Label>
                <Select
                  value={settings.llmProvider}
                  onValueChange={(v: string) => {
                    // s17 用户合同：火山引擎 → 自动填 doubao-seed-2-0-pro-260215；
                    // 其它 provider → 模型框置空，让用户主动填（不替用户预选）。
                    const next = v as LLMProvider;
                    settings.setSettings({
                      llmProvider: next,
                      llmModel:
                        next === "doubao" ? "doubao-seed-2-0-pro-260215" : "",
                    });
                  }}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {/* s12：使用规范厂商名 */}
                    <SelectItem value="doubao">火山引擎</SelectItem>
                    <SelectItem value="deepseek">DeepSeek</SelectItem>
                    <SelectItem value="qwen">阿里云百炼</SelectItem>
                    <SelectItem value="glm">智谱</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label>模型</Label>
                <Input
                  value={settings.llmModel}
                  onChange={(e) =>
                    settings.setSettings({ llmModel: e.target.value })
                  }
                  placeholder={
                    PROVIDER_DEFAULT_MODELS[settings.llmProvider] ||
                    ""
                  }
                  data-testid="input-llm-model"
                />
              </div>
            </div>
            <div className="space-y-1.5">
              <Label>API Key</Label>
              <Input
                type="password"
                value={settings.llmKey}
                onChange={(e) =>
                  settings.setSettings({ llmKey: e.target.value })
                }
                placeholder={
                  ""
                }
                data-testid="input-llm-key"
              />
            </div>

            <div className="border-t pt-3 mt-3 space-y-3">
              <p className="text-xs text-muted-foreground">
                火山引擎语音（``api/v3/tts/unidirectional`` + ``sauc/bigmodel_async``，TTS + STT 共用）
              </p>
              <div className="space-y-1.5">
                <Label>火山引擎语音 API Key</Label>
                <Input
                  type="password"
                  value={settings.volcVoiceKey}
                  onChange={(e) =>
                    settings.setSettings({ volcVoiceKey: e.target.value })
                  }
                  placeholder=""
                  data-testid="input-volc-voice-key"
                />
                <p className="text-[11px] text-muted-foreground">
                  获取地址：火山引擎控制台 → 语音技术 → 应用管理 → API Key（同一把 Key 既能调 TTS 也能调 STT）。
                  未配置时不出 AI 语音，且无法用麦克风作答（仍可用文本作答）。
                  Key 仅保留在本标签页内存里，关闭页面即销毁，**不**写入 localStorage。
                </p>
              </div>
              {/* v0.4：旧的 DashScope / 双头火山字段已退役；UI 不再暴露，
                  老 store / localStorage 中残留的字段仍保留以避免破坏其它
                  代码路径，新业务流不会读它们。 */}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>面试模式</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-2">
              {INTERVIEW_TYPES.map((it) => (
                <button
                  key={it.value}
                  onClick={() => setInterviewType(it.value)}
                  data-testid={`type-${it.value}`}
                  className={`text-left rounded-lg border p-3 transition-colors ${
                    interviewType === it.value
                      ? "border-accent ring-2 ring-accent/40 bg-accent/5"
                      : "hover:border-accent/60"
                  }`}
                >
                  <div className="font-medium text-sm">{it.label}</div>
                  <div className="text-xs text-muted-foreground mt-1">
                    {it.desc}
                  </div>
                </button>
              ))}
            </div>
            <div className="space-y-1.5">
              <Label>评价模式</Label>
              <Select
                value={evalMode}
                onValueChange={(v: string) => setEvalMode(v as EvalMode)}
              >
                <SelectTrigger data-testid="select-eval-mode">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="realtime" data-testid="eval-mode-realtime">
                    实时评价（每轮即时反馈）
                  </SelectItem>
                  <SelectItem value="summary" data-testid="eval-mode-summary">
                    整体评价（结束后统一反馈）
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* 右：岗位 + 简历 + 启动 */}
      <div className="space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>选择岗位</CardTitle>
            <CardDescription>
              下方为腾讯 / 字节 / 阿里官网实时抓取的 AI 岗位（带原始链接）。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Tabs defaultValue="library">
              <TabsList>
                <TabsTrigger value="library">大厂岗位库</TabsTrigger>
                <TabsTrigger value="custom">自定义岗位</TabsTrigger>
              </TabsList>
              <TabsContent value="library">
                <JobPicker
                  selectedId={job?.id}
                  onSelect={(j) => {
                    setJob(j);
                    setManualJobTitle("");
                  }}
                />
              </TabsContent>
              <TabsContent value="custom" className="space-y-3">
                <div className="space-y-1.5">
                  <Label>岗位名称</Label>
                  <Input
                    value={manualJobTitle}
                    onChange={(e) => {
                      setManualJobTitle(e.target.value);
                      setJob(null);
                    }}
                    placeholder="如：大模型算法工程师"
                    data-testid="input-job-title"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label>岗位 JD（职责 + 要求）</Label>
                  <Textarea
                    value={manualJobJD}
                    onChange={(e) => setManualJobJD(e.target.value)}
                    placeholder="粘贴岗位职责与要求..."
                    rows={6}
                  />
                </div>
              </TabsContent>
            </Tabs>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>上传简历（可选）</CardTitle>
            <CardDescription>
              支持 PDF / TXT / MD，单文件不超过 {RESUME_MAX_MB} MB；
              将由 LLM 抽取关键画像，作为面试上下文。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex items-center gap-3">
              <label className="cursor-pointer">
                <input
                  type="file"
                  accept=".pdf,.txt,.md"
                  onChange={onUploadResume}
                  className="hidden"
                  data-testid="input-resume-file"
                />
                <span className="inline-flex items-center gap-2 rounded-md border border-input bg-background px-3 h-10 text-sm hover:bg-accent hover:text-accent-foreground">
                  {uploading ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <FileUp className="h-4 w-4" />
                  )}
                  选择简历文件
                </span>
              </label>
              {resumeFilename && (
                <Badge variant="success">{resumeFilename}</Badge>
              )}
            </div>
            <Textarea
              value={resumeText}
              onChange={(e) => setResumeText(e.target.value)}
              placeholder="或直接粘贴简历摘要..."
              rows={5}
              data-testid="input-resume-text"
            />
          </CardContent>
        </Card>

        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
            {error}
          </div>
        )}

        <div className="flex justify-end gap-3">
          <Button
            size="lg"
            onClick={startInterview}
            disabled={creating}
            data-testid="btn-start-interview"
          >
            {creating ? (
              <>
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                进入面试中…
              </>
            ) : (
              <>
                <PlayCircle className="h-4 w-4 mr-2" />
                开始模拟面试
              </>
            )}
          </Button>
        </div>
        <p className="text-xs text-muted-foreground text-right inline-flex items-center gap-1 ml-auto">
          <Mic className="h-3 w-3" /> 进入面试后浏览器会请求麦克风权限
        </p>
      </div>
    </div>
  );
}
