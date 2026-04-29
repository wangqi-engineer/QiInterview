import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScoreChart } from "@/components/ScoreChart";
import {
  apiClient,
  REPORT_FIELDS,
  streamReport,
  type InterviewDetail,
  type ReportField,
  type ReportOut,
} from "@/lib/api";
import {
  ArrowLeft,
  ExternalLink,
  Loader2,
  RefreshCcw,
  TrendingDown,
  TrendingUp,
} from "lucide-react";

const EMPTY_SECTIONS: Record<ReportField, string> = {
  summary: "",
  strengths_md: "",
  weaknesses_md: "",
  advice_md: "",
  score_explanation_md: "",
};

export default function ReportPage() {
  const { sid = "" } = useParams<{ sid: string }>();
  const nav = useNavigate();
  const [info, setInfo] = useState<InterviewDetail | null>(null);
  const [report, setReport] = useState<ReportOut | null>(null);
  const [sections, setSections] = useState<Record<ReportField, string>>(EMPTY_SECTIONS);
  const [streaming, setStreaming] = useState(false);
  const [firstChunkAt, setFirstChunkAt] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const streamSeqRef = useRef(0);

  const fallbackLoad = async () => {
    const [d, r] = await Promise.all([
      apiClient.getInterview(sid),
      apiClient.getReport(sid),
    ]);
    setInfo(d);
    setReport(r);
    setSections({
      summary: r.summary,
      strengths_md: r.strengths_md,
      weaknesses_md: r.weaknesses_md,
      advice_md: r.advice_md,
      score_explanation_md:
        ((r as unknown as { score_explanation_md?: string }).score_explanation_md) || "",
    });
  };

  /** 优先 SSE 流式；3.5 秒内没收到首块或 SSE 失败时回退普通 GET。 */
  const load = async () => {
    abortRef.current?.abort();
    const ctl = new AbortController();
    abortRef.current = ctl;
    const seq = ++streamSeqRef.current;

    setLoading(true);
    setStreaming(true);
    setError("");
    setSections(EMPTY_SECTIONS);
    setFirstChunkAt(null);
    setReport(null);

    let infoLoaded = false;
    apiClient
      .getInterview(sid)
      .then((d) => {
        if (streamSeqRef.current === seq) {
          setInfo(d);
          infoLoaded = true;
        }
      })
      .catch(() => {});

    const start = performance.now();
    const fallbackTimer = setTimeout(() => {
      // 3.5s 还没首块 → 抛弃 SSE，走 GET 回退
      if (streamSeqRef.current === seq && firstChunkAt === null) {
        ctl.abort();
      }
    }, 3500);

    try {
      const accum: Record<ReportField, string> = { ...EMPTY_SECTIONS };
      let gotFirst = false;
      for await (const ev of streamReport(sid, ctl.signal)) {
        if (streamSeqRef.current !== seq) break;
        if (!gotFirst) {
          gotFirst = true;
          setFirstChunkAt(performance.now() - start);
          setLoading(false);
          clearTimeout(fallbackTimer);
        }
        if (ev.type === "section_delta") {
          accum[ev.section] = (accum[ev.section] || "") + ev.delta;
          setSections({ ...accum });
        } else if (ev.type === "done") {
          for (const f of REPORT_FIELDS) {
            accum[f] = ev.data[f] ?? accum[f] ?? "";
          }
          setSections({ ...accum });
          setReport({
            session_id: sid,
            summary: accum.summary,
            strengths_md: accum.strengths_md,
            weaknesses_md: accum.weaknesses_md,
            advice_md: accum.advice_md,
            trend: ev.trend,
            turns: [],
            created_at: new Date().toISOString(),
          });
          // info 里有 turns；done 里没有 turns，单独再 GET 一次拿到 turn 列表
          try {
            const r = await apiClient.getReport(sid);
            if (streamSeqRef.current === seq) setReport(r);
          } catch {
            // 已经能渲染主体内容，忽略
          }
        } else if (ev.type === "error") {
          throw new Error(ev.message);
        }
      }
      clearTimeout(fallbackTimer);
      setStreaming(false);
      setLoading(false);
    } catch (e: any) {
      clearTimeout(fallbackTimer);
      if (streamSeqRef.current !== seq) return;
      // SSE 失败 → 普通 GET 兜底
      try {
        await fallbackLoad();
        setError("");
      } catch (e2: any) {
        if (!infoLoaded) setInfo(null);
        setError(e2?.response?.data?.detail || e2.message || e.message);
      } finally {
        setStreaming(false);
        setLoading(false);
      }
    }
  };

  useEffect(() => {
    void load();
    return () => {
      abortRef.current?.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid]);

  const onRegen = async () => {
    await apiClient.regenReport(sid);
    void load();
  };

  if (loading) {
    return (
      <div className="text-center py-20 text-muted-foreground">
        <Loader2 className="h-6 w-6 mx-auto mb-3 animate-spin" />
        正在生成复盘报告（首次需要 LLM 计算，首屏一般 2-3s）...
      </div>
    );
  }
  if (error && !info) {
    return (
      <div className="text-center py-20">
        <div className="text-destructive">{error}</div>
        <Button onClick={load} className="mt-4">
          重试
        </Button>
      </div>
    );
  }
  if (!info) return null;
  // 流式过程中 report 还没拼好的占位
  const reportView: ReportOut =
    report ?? {
      session_id: sid,
      summary: sections.summary,
      strengths_md: sections.strengths_md,
      weaknesses_md: sections.weaknesses_md,
      advice_md: sections.advice_md,
      trend: [],
      turns: [],
      created_at: new Date().toISOString(),
    };

  const finalScore = info.final_score;
  const initial = info.initial_score;
  const trend = reportView.trend;
  const candidateTurns = (reportView.turns || []).filter((t) => t.role === "candidate");
  // #region agent log
  if (typeof window !== "undefined") {
    const _allTurns = reportView.turns || [];
    const _interviewer = _allTurns.filter((t) => t.role === "interviewer");
    const _ai_snippets = _interviewer.slice(0, 4).map((t) => ({
      idx: t.idx,
      strategy: (t as { strategy?: string }).strategy ?? null,
      tlen: (t.text || "").length,
      head: (t.text || "").slice(0, 14),
    }));
    fetch("http://127.0.0.1:7756/ingest/9de27574-9d67-4459-85aa-d570f039638a", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Debug-Session-Id": "714cc8" },
      body: JSON.stringify({
        sessionId: "714cc8",
        runId: `report_${sid.slice(0, 6)}_${Date.now()}`,
        hypothesisId: "H11-A-DATA",
        location: "ReportPage.tsx:209",
        message: "reportView.turns observed",
        data: {
          total: _allTurns.length,
          candidate_n: candidateTurns.length,
          interviewer_n: _interviewer.length,
          ai_head_samples: _ai_snippets,
        },
        timestamp: Date.now(),
      }),
    }).catch(() => {});
  }
  // #endregion
  const positives = trend.filter((t) => t.delta > 0).length;
  const negatives = trend.filter((t) => t.delta < 0).length;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <Button variant="ghost" onClick={() => nav("/history")}>
          <ArrowLeft className="h-4 w-4 mr-2" /> 返回列表
        </Button>
        <div className="flex items-center gap-2">
          {streaming && (
            <Badge variant="secondary" className="text-xs">
              <Loader2 className="h-3 w-3 mr-1 animate-spin" />
              流式生成中
              {firstChunkAt !== null && ` · 首块 ${(firstChunkAt / 1000).toFixed(1)}s`}
            </Badge>
          )}
          <Button variant="outline" onClick={onRegen} data-testid="btn-regen-report">
            <RefreshCcw className="h-4 w-4 mr-2" /> 重新生成报告
          </Button>
        </div>
      </div>

      <div className="grid lg:grid-cols-[1fr_420px] gap-6">
        <Card>
          <CardHeader>
            <CardTitle>分数趋势</CardTitle>
            <CardDescription>
              印象分 → 各轮回答打分变化（红线为熔断阈值）
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ScoreChart data={trend} initial={initial} />
            <div className="grid grid-cols-3 gap-3 mt-4 text-center">
              <Stat label="初始印象分" value={String(initial)} />
              <Stat label="最终得分" value={String(finalScore)} highlight />
              <Stat label="净变化" value={`${finalScore - initial > 0 ? "+" : ""}${finalScore - initial}`} />
            </div>
            <div className="flex justify-center gap-3 mt-3 text-xs text-muted-foreground">
              <Badge variant="success">
                <TrendingUp className="h-3 w-3 mr-1" />
                加分轮次 {positives}
              </Badge>
              <Badge variant="destructive">
                <TrendingDown className="h-3 w-3 mr-1" />
                扣分轮次 {negatives}
              </Badge>
              <Badge variant="outline">总轮 {candidateTurns.length}</Badge>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>面试概览</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            <div>
              <span className="text-muted-foreground">岗位：</span>
              {info.job_title}
              {info.job_url && (
                <a
                  href={info.job_url}
                  target="_blank"
                  rel="noreferrer"
                  className="ml-2 inline-flex items-center text-accent hover:underline text-xs"
                >
                  原页 <ExternalLink className="h-3 w-3 ml-0.5" />
                </a>
              )}
            </div>
            <div>
              <span className="text-muted-foreground">面试类型：</span>
              {info.interview_type}
            </div>
            <div>
              <span className="text-muted-foreground">模型：</span>
              {info.llm_provider} / {info.llm_model}
            </div>
            <div>
              <span className="text-muted-foreground">音色：</span>
              {info.voice_speaker}
            </div>
            <div>
              <span className="text-muted-foreground">结束原因：</span>
              {info.end_reason || "-"}
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="grid md:grid-cols-3 gap-4">
        <ReportSection
          title="整体总结"
          body={sections.summary}
          streaming={streaming && !sections.summary}
        />
        <ReportSection
          title="亮点"
          body={sections.strengths_md}
          variant="success"
          streaming={streaming && !sections.strengths_md}
        />
        <ReportSection
          title="不足"
          body={sections.weaknesses_md}
          variant="warn"
          streaming={streaming && !sections.weaknesses_md}
        />
      </div>
      <ReportSection
        title="提升建议"
        body={sections.advice_md}
        streaming={streaming && !sections.advice_md}
      />
      {sections.score_explanation_md && (
        <ReportSection title="评分解释" body={sections.score_explanation_md} />
      )}

      <Card>
        <CardHeader>
          <CardTitle>逐轮点评</CardTitle>
          <CardDescription>展开查看每轮回答的优点 / 不足 / 参考答案</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {candidateTurns.map((t) => {
            const priorInterviewer = (reportView.turns || [])
              .filter(
                (x) => x.role === "interviewer" && x.idx < t.idx && (x.text || "").length > 0,
              )
              .sort((a, b) => b.idx - a.idx)[0];
            // #region agent log
            if (typeof window !== "undefined") {
              fetch("http://127.0.0.1:7756/ingest/9de27574-9d67-4459-85aa-d570f039638a", {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-Debug-Session-Id": "714cc8" },
                body: JSON.stringify({
                  sessionId: "714cc8",
                  runId: "post-fix",
                  hypothesisId: "H11-B-UI-MISSING-QUESTION",
                  location: "ReportPage.tsx:turn-render",
                  message: "props passed to TurnDetail (post-fix: question paired)",
                  data: {
                    idx: t.idx,
                    pairedQuestionIdx: priorInterviewer?.idx ?? null,
                    pairedQuestionLen: (priorInterviewer?.text || "").length,
                    pairedQuestionHead: (priorInterviewer?.text || "").slice(0, 14),
                    propKeys: ["idx", "text", "delta", "total", "evaluator", "question"],
                    has_question_prop: true,
                  },
                  timestamp: Date.now(),
                }),
              }).catch(() => {});
            }
            // #endregion
            return (
              <TurnDetail
                key={t.id}
                idx={t.idx}
                text={t.text}
                delta={t.score_delta}
                total={t.score_after}
                evaluator={t.evaluator_json as Record<string, string> | null}
                question={priorInterviewer?.text || ""}
                questionStrategy={
                  (priorInterviewer as { strategy?: string } | undefined)?.strategy ?? null
                }
              />
            );
          })}
          {candidateTurns.length === 0 && (
            <div className="text-center text-muted-foreground py-8">
              该面试没有候选人回答记录
            </div>
          )}
        </CardContent>
      </Card>

      <div className="text-center pt-4">
        <Link to="/setup" className="text-sm text-accent hover:underline">
          再来一次模拟面试 →
        </Link>
      </div>
    </div>
  );
}

function Stat({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  return (
    <div className="rounded-md border p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`text-2xl font-semibold mt-1 ${highlight ? "text-accent" : ""}`}>
        {value}
      </div>
    </div>
  );
}

function ReportSection({
  title,
  body,
  variant,
  streaming,
}: {
  title: string;
  body: string;
  variant?: "success" | "warn";
  streaming?: boolean;
}) {
  const cls =
    variant === "success"
      ? "border-emerald-500/30 bg-emerald-500/5"
      : variant === "warn"
        ? "border-amber-500/30 bg-amber-500/5"
        : "";
  return (
    <Card className={cls}>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          {title}
          {streaming && (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
          )}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="text-sm whitespace-pre-wrap leading-relaxed">
          {body || (streaming ? "（生成中…）" : "（无）")}
        </div>
      </CardContent>
    </Card>
  );
}

function TurnDetail({
  idx,
  text,
  delta,
  total,
  evaluator,
  question,
  questionStrategy,
}: {
  idx: number;
  text: string;
  delta: number;
  total: number;
  evaluator?: Record<string, any> | null;
  question?: string;
  questionStrategy?: string | null;
}) {
  const hasQuestion = (question || "").trim().length > 0;
  return (
    <details className="rounded-md border p-3" data-testid={`turn-detail-${idx}`}>
      <summary className="cursor-pointer flex items-center gap-2">
        <Badge variant="outline">第 {idx} 轮</Badge>
        <span className="text-sm truncate flex-1">{text.slice(0, 80)}</span>
        <Badge
          variant={delta > 0 ? "success" : delta < 0 ? "destructive" : "outline"}
        >
          {delta > 0 ? `+${delta}` : delta} → {total}
        </Badge>
      </summary>
      <div className="mt-3 space-y-2 text-sm">
        {hasQuestion && (
          <div
            className="rounded border border-blue-500/30 bg-blue-500/5 p-2"
            data-testid={`turn-question-${idx}`}
          >
            <div className="flex items-center gap-2 mb-1">
              <Badge variant="secondary" className="text-[10px]">
                AI 提问
              </Badge>
              {questionStrategy && (
                <span className="text-[10px] text-muted-foreground uppercase tracking-wide">
                  {questionStrategy}
                </span>
              )}
            </div>
            <div className="whitespace-pre-wrap leading-relaxed">{question}</div>
          </div>
        )}
        <div
          className="rounded bg-muted/40 p-2 whitespace-pre-wrap"
          data-testid={`turn-answer-${idx}`}
        >
          <div className="flex items-center gap-2 mb-1">
            <Badge variant="outline" className="text-[10px]">
              我作答
            </Badge>
          </div>
          <div>{text}</div>
        </div>
        {evaluator && (
          <div className="grid md:grid-cols-3 gap-2 text-xs">
            <div className="p-2 rounded border border-emerald-500/30 bg-emerald-500/5">
              <div className="font-medium text-emerald-700 mb-1">优点</div>
              <div>{evaluator.strengths || "-"}</div>
            </div>
            <div className="p-2 rounded border border-amber-500/30 bg-amber-500/5">
              <div className="font-medium text-amber-700 mb-1">不足</div>
              <div>{evaluator.weaknesses || "-"}</div>
            </div>
            <div className="p-2 rounded border border-blue-500/30 bg-blue-500/5">
              <div className="font-medium text-blue-700 mb-1">参考</div>
              <div>{evaluator.reference || "-"}</div>
            </div>
          </div>
        )}
      </div>
    </details>
  );
}
