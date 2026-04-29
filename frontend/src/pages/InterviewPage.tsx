import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { WaveAnimation, type WaveState } from "@/components/WaveAnimation";
import { apiClient, type InterviewDetail } from "@/lib/api";
import { InterviewWS, type ServerEvent } from "@/lib/ws";
import {
  AudioCapture,
  arrayBufferToBase64,
  base64ToArrayBuffer,
  type AudioFrame,
} from "@/lib/audioCapture";
import { StreamingAudioPlayer } from "@/lib/audioPlayer";
import {
  CheckCircle2,
  Mic,
  MicOff,
  Play,
  Send,
  StopCircle,
  Volume2,
} from "lucide-react";

interface DialogTurn {
  role: "interviewer" | "candidate";
  text: string;
  strategy?: string;
  delta?: number;
  total?: number;
  evaluator?: Record<string, unknown>;
  interrupt?: boolean;
}

// #region agent log
const _DBG_RUN = `qi_${Math.random().toString(36).slice(2, 8)}_${Date.now()}`;
const _DBG_C = { wsEnter: 0, wsClean: 0, impEnter: 0, tick: 0, onClose: 0 };
const _dbg = (
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
        runId: _DBG_RUN,
        hypothesisId: "H1-WS-RECONNECT-STORM",
        location,
        message,
        data,
        timestamp: Date.now(),
      }),
    }
  ).catch(() => {});
};
// #endregion

const TYPE_LABEL: Record<string, string> = {
  tech1: "技术一面",
  tech2: "技术二面",
  comprehensive: "综合面",
  hr: "HR 面",
};

export default function InterviewPage() {
  const { sid = "" } = useParams<{ sid: string }>();
  const nav = useNavigate();

  const [info, setInfo] = useState<InterviewDetail | null>(null);
  const [waveState, setWaveState] = useState<WaveState>("idle");
  const [turns, setTurns] = useState<DialogTurn[]>([]);
  const [partial, setPartial] = useState("");
  const [score, setScore] = useState<number>(0);
  const [scoreDelta, setScoreDelta] = useState<number>(0);
  const [micOn, setMicOn] = useState(false);
  const [recording, setRecording] = useState(false);
  const [textAnswer, setTextAnswer] = useState("");
  const [error, setError] = useState("");
  const [ended, setEnded] = useState(false);
  const [endReason, setEndReason] = useState("");
  // 进入面试页后弹窗要求先授权麦克风，micPrimed=true 才允许 WS
  // 连接 + 发 start 触发开场。规避“先 TTS 后 STT 导致 STT 异常”的时序问题。
  const [micPrimed, setMicPrimed] = useState(false);
  const [primingMic, setPrimingMic] = useState(false);

  const wsRef = useRef<InterviewWS | null>(null);
  const captureRef = useRef<AudioCapture | null>(null);
  const playerRef = useRef<StreamingAudioPlayer | null>(null);
  const audioElRef = useRef<HTMLAudioElement | null>(null);
  const speakingFramesRef = useRef<number>(0);
  const sendingRef = useRef(false);
  const dialogPaneRef = useRef<HTMLDivElement | null>(null);
  const recordingRef = useRef(false);
  const waveStateRef = useRef<WaveState>("idle");
  const micOnRef = useRef(false);
  // v0.4 / 用户合同：『新一轮录音的识别文字应当 *追加* 到 textarea，而不是
  // 覆盖』。STT partial 是当前这次按下 [开始录音] 之后的"草稿"；点 [结束录音]
  // 时把它（如果还没被 final 替换）作为该轮的 commit 拼到 ``committedSttPrefix``
  // 之后。下一次 [开始录音] 看到 prefix 非空就把 partial / final 都拼在 prefix
  // 之后写回 textarea，不再清空已有内容。点 [发送] 后清 prefix 与 textarea，
  // 进入新一轮回答。
  const committedSttPrefixRef = useRef<string>("");
  const [committedSttPrefix, setCommittedSttPrefix] = useState<string>("");
  useEffect(() => {
    committedSttPrefixRef.current = committedSttPrefix;
  }, [committedSttPrefix]);
  // v0.5：AudioCapture onSpeakingEnd 回调是闭包，拿不到最新 React state。
  // 用 ref 同步 partial 值，供 VAD 自动定稿时读取当前草稿文字。
  const partialRef = useRef<string>("");
  useEffect(() => {
    partialRef.current = partial;
  }, [partial]);

  useEffect(() => {
    recordingRef.current = recording;
  }, [recording]);
  useEffect(() => {
    waveStateRef.current = waveState;
  }, [waveState]);
  useEffect(() => {
    micOnRef.current = micOn;
  }, [micOn]);

  useEffect(() => {
    const el = dialogPaneRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns, partial]);

  // 加载面试基本信息
  useEffect(() => {
    apiClient.getInterview(sid).then((d) => {
      setInfo(d);
      setScore(d.initial_score);
      // 恢复历史轮次
      const past: DialogTurn[] = d.turns.map((t) => ({
        role: t.role as "interviewer" | "candidate",
        text: t.text,
        strategy: t.strategy ?? undefined,
        delta: t.score_delta,
        total: t.score_after,
        evaluator: (t.evaluator_json as Record<string, unknown>) ?? undefined,
      }));
      setTurns(past);
      if (d.ended_at) {
        setEnded(true);
        setEndReason(d.end_reason || "complete");
      }
    });
  }, [sid]);

  // 印象分异步路径：若 status=pending 则每 1.5s 轮询一次直到 ready
  useEffect(() => {
    // #region agent log
    _DBG_C.impEnter += 1;
    _dbg("InterviewPage.tsx:impEffectEntry", "imp poll effect run", {
      n: _DBG_C.impEnter,
      hasInfo: !!info,
      infoId: info?.id,
      status: info?.impression_breakdown?.status,
    });
    // #endregion
    if (!info) return;
    const status = info.impression_breakdown?.status;
    if (status !== "pending") return;
    let stopped = false;
    const tick = async () => {
      if (stopped) return;
      try {
        const fresh = await apiClient.getInterview(sid);
        if (stopped) return;
        // #region agent log
        _DBG_C.tick += 1;
        _dbg("InterviewPage.tsx:impTick", "imp tick fetched, about to setInfo", {
          n: _DBG_C.tick,
          freshId: fresh.id,
          freshStatus: fresh.impression_breakdown?.status,
        });
        // #endregion
        setInfo(fresh);
        // 印象分一旦到位且当前 score 仍是初始 fallback，更新右上角分数显示
        if (
          fresh.impression_breakdown?.status === "ready" &&
          score === info.initial_score
        ) {
          setScore(fresh.initial_score);
        }
        if (fresh.impression_breakdown?.status === "pending") {
          setTimeout(tick, 1500);
        }
      } catch {
        if (!stopped) setTimeout(tick, 2500);
      }
    };
    const t = setTimeout(tick, 800);
    return () => {
      stopped = true;
      clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [info?.id, info?.impression_breakdown?.status]);

  // 建立 WebSocket
  useEffect(() => {
    // #region agent log
    _DBG_C.wsEnter += 1;
    _dbg("InterviewPage.tsx:wsEffectEntry", "WS effect run", {
      n: _DBG_C.wsEnter,
      hasInfo: !!info,
      infoId: info?.id,
      ended,
    });
    // #endregion
    // micPrimed 之前不建立 WS：待用户在进面试弹窗里先点开麦才启动。
    if (!info || ended || !micPrimed) return;
    const ws = new InterviewWS({
      sid,
      onEvent: handleEvent,
      onClose: () => {
        // #region agent log
        _DBG_C.onClose += 1;
        _dbg("InterviewPage.tsx:wsOnClose", "WS onClose handler fired", {
          n: _DBG_C.onClose,
        });
        // #endregion
        /* 可以在这里加重连 */
      },
    });
    wsRef.current = ws;
    ws.connect()
      .then(() => {
        // 如果还没有任何 interviewer 发言，触发开场
        const hasOpening = info.turns.some((t) => t.role === "interviewer");
        // #region agent log
        _dbg("InterviewPage.tsx:wsConnectResolved", "ws.connect() resolved", {
          hasOpening,
          turnsLen: info.turns.length,
          willStart: !hasOpening,
        });
        // #endregion
        if (!hasOpening) {
          ws.start();
          // #region agent log
          _dbg("InterviewPage.tsx:wsStartSent", "ws.start() invoked", {});
          // #endregion
        }
      })
      .catch((e) => {
        // #region agent log
        _dbg("InterviewPage.tsx:wsConnectError", "ws.connect() rejected", {
          err: String(e?.message || e),
        });
        // #endregion
        setError("WS 连接失败：" + e.message);
      });
    return () => {
      // #region agent log
      _DBG_C.wsClean += 1;
      _dbg("InterviewPage.tsx:wsCleanup", "WS effect cleanup, closing ws", {
        n: _DBG_C.wsClean,
      });
      // #endregion
      ws.close();
      stopMic();
    };
    // 关键修复（缺陷 #5 / H1-WS-RECONNECT-STORM）：
    // 仅在"会话 id 切换"或"面试结束"时重建 WS。
    // 之前依赖 [info] 会导致印象分轮询 (`setInfo(fresh)`) 每 ~1.5s 改变
    // info 对象引用 → cleanup → 立刻重连，TTS 首字音频帧根本来不及到达前端，
    // 同时偶尔会让 user 的文本作答因恰逢半关闭窗口而被丢弃。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [info?.id, ended, micPrimed]);

  const handleEvent = async (ev: ServerEvent) => {
    switch (ev.type) {
      case "ai_thinking":
        setWaveState("thinking");
        break;
      case "ai_text":
        setTurns((prev) => [
          ...prev,
          { role: "interviewer", text: ev.text, strategy: ev.strategy },
        ]);
        // v0.7：AI 每说一句自动服务器端 TTS，省去用户手动点「朗读 AI
        // 最新一句」的操作。后端 opening / next_question / wrap_up 均走
        // auto_tts=False（见 voice_ws.py::_drive_speech_stream），不会与此处重复。
        // 保留按钮侜用于「重听」场景。
        if (ev.text) {
          speakAiText(ev.text);
        }
        break;
      case "ai_audio":
        // D14 / VOICE-LATENCY-EAGER-FILLER-CACHE：``filler:true`` 帧（来自后端
        // ``_play_filler_oneshot:cache_hit``）是一段独立完整的 mp3。MediaSource
        // 路径在 headless Chromium 上对单帧 ~10 KB mp3 不稳定地触发 ``playing``
        // 事件（实测 5-7 s 后才进入 HAVE_ENOUGH_DATA），把 i5/i8 的 1s 预算吃光。
        // 走一次性 ``new Audio(blob:URL)`` 解码全文件后立即触发原生
        // ``playing`` 事件（实测 <100 ms），同时不占用 MediaSource 队列——
        // 后续 LLM-driven TTS 的连续 mp3 流照常通过 ``StreamingAudioPlayer``
        // 喂给 ``audioElRef``。
        if (ev.filler) {
          try {
            const buf = base64ToArrayBuffer(ev.chunk_b64);
            const blob = new Blob([buf], { type: ev.mime || "audio/mpeg" });
            const url = URL.createObjectURL(blob);
            const a = new Audio(url);
            a.preload = "auto";
            a.addEventListener("ended", () => URL.revokeObjectURL(url));
            void a.play().catch(() => {
              try { URL.revokeObjectURL(url); } catch { /* noop */ }
            });
          } catch { /* noop */ }
          setWaveState("speaking");
          break;
        }
        if (!playerRef.current) {
          const audio = audioElRef.current!;
          playerRef.current = new StreamingAudioPlayer(audio);
          await playerRef.current.start();
        }
        playerRef.current.appendBase64(ev.chunk_b64);
        setWaveState("speaking");
        break;
      case "ai_audio_end": {
        const player = playerRef.current;
        if (!player) {
          setWaveState(micOnRef.current ? "listening" : "idle");
          break;
        }
        // 打断场景（用户插话 / 后端主动 cancel_tts）：立即停，不等自然播完。
        // 原逻辑用 setTimeout(200) 一刀切 reset，会把还在 SourceBuffer 里缓冲的
        // 几秒音频连同 ``<audio>`` 一起 pause + endOfStream("decode")，导致开场白/
        // next_question 这种较长文本听起来被掐掉。改为：先 endOfStream() 封口
        // MSE，再 await ``<audio>`` 的 ``ended`` 事件，真的播完才 reset。
        if (ev.interrupted) {
          player.reset();
          playerRef.current = null;
          setWaveState(micOnRef.current ? "listening" : "idle");
          break;
        }
        player.endOfStream();
        void player.waitUntilEnded().then(() => {
          // 等待期间若已被新一轮 start()/reset() 换成别的 player 实例，则不要
          // 动最新那个；只回收我们捕获到的这一段。
          if (playerRef.current === player) {
            player.reset();
            playerRef.current = null;
          }
          setWaveState(micOnRef.current ? "listening" : "idle");
        });
        break;
      }
      case "stt_partial":
        // v0.4：partial 文字是当前这次录音的"草稿"，把它**追加**到
        // ``committedSttPrefix`` 之后写回 textarea。``prefix`` 在用户点
        // [开始录音]→[结束录音] 时累积，[发送本轮回答] 后才清空。这样
        // 同一轮里多次开/停录音不会丢前面已说过的话。
        setPartial(ev.text);
        if (ev.text) {
          const prefix = committedSttPrefixRef.current;
          const merged = prefix
            ? prefix.replace(/\s+$/u, "") + (prefix.endsWith("。") ? "" : " ") + ev.text
            : ev.text;
          setTextAnswer(merged);
        }
        // #region agent log
        _dbg("InterviewPage.tsx:stt_partial", "appended stt_partial to textarea", {
          textLen: ev.text?.length ?? 0,
          prefixLen: committedSttPrefixRef.current.length,
        });
        // #endregion
        break;
      case "stt_final":
        // v0.4 / i16：final 之后**不再**自动 append 一条 candidate turn —— 后端
        // 在新合同里也不会自动 _on_user_final、不会推 next_question。final 文本
        // 落到 textarea 与 ``committedSttPrefix``，让用户确认 / 编辑后手动点
        // [发送本轮回答]。同一轮里多次开/停录音都会被并入 prefix。
        if (ev.text) {
          const prefix = committedSttPrefixRef.current;
          const merged = prefix
            ? prefix.replace(/\s+$/u, "") + (prefix.endsWith("。") ? "" : " ") + ev.text
            : ev.text;
          setTextAnswer(merged);
          // final 等于本次开/停录音的最终结果——把它合入 prefix，下一次开
          // 录音时 partial 会接在它之后。
          committedSttPrefixRef.current = merged;
          setCommittedSttPrefix(merged);
        }
        setPartial("");
        // #region agent log
        _dbg("InterviewPage.tsx:stt_final", "appended stt_final to textarea + prefix", {
          textLen: ev.text?.length ?? 0,
          newPrefixLen: committedSttPrefixRef.current.length,
        });
        // #endregion
        break;
      case "score_update":
        setScore(ev.total);
        setScoreDelta(ev.delta);
        setTurns((prev) => {
          const next = [...prev];
          for (let i = next.length - 1; i >= 0; i--) {
            if (next[i].role === "candidate") {
              next[i] = {
                ...next[i],
                delta: ev.delta,
                total: ev.total,
                evaluator: ev.evaluator,
              };
              break;
            }
          }
          return next;
        });
        break;
      case "ai_interrupt":
        // 后端仍会发这个信号（用于打分语义），但不再在会话框渲染提示气泡。
        break;
      case "interview_end":
        setEnded(true);
        setEndReason(ev.reason);
        setWaveState("idle");
        stopMic();
        break;
      case "error":
        setError(ev.message);
        break;
    }
  };

  // i13 / i16 / 语音手动化（v0.3）：
  // 起麦 == "开始录音"；不再装 VAD callback；停麦 != 提交。
  //   - 点 [开始录音] → startMic() → 持续上送 audio_chunk；后端 STT
  //     用 stt_partial / stt_final 把识别文字写到 [input-text-answer]；
  //   - 点 [结束录音] → stopMic() → 仅停止麦克风采集，**不**发 end_turn、
  //     **不**推进 AI；
  //   - 用户在 textarea 编辑/修正后点 [发送本轮回答] → 走 sendAnswerText
  //     → answer_text 上行 → 后端 _on_user_final → next_question。
  // 这里曾装的 ``onSpeakingStart → wsRef.interrupt()`` /
  // ``onSpeakingEnd → wsRef.endTurn()`` 自动闭环已删除：它会让 AI
  // 在用户说话期间被"自动打断"或"被 VAD 提前 endTurn 抢话"，
  // 直接违反 i13/i16 的协议级断言。
  const startMic = async () => {
    setError("");
    if (captureRef.current) return;
    try {
      const cap = new AudioCapture({
        rmsSpeakingThreshold: 0.025,
        rmsSilenceMs: 700,
        onFrame: (f: AudioFrame) => {
          if (!recordingRef.current) return;
          const ws = wsRef.current;
          if (!ws) return;
          const b64 = arrayBufferToBase64(f.pcm);
          ws.sendAudioFrame(b64);
        },
        // v0.5 / 用户合同：VAD 检测到本句说完（rmsSilenceMs 静音）时
        // 自动把当前 partial 提升为 prefix，等价于用户虚拟地点了一下
        // 「结束录音→开始录音」，但不断麦、不断 WS、不推进 AI。
        // 关键：仅修改本地 prefix/partial state，不调 wsRef.endTurn() /
        // wsRef.interrupt()，因此永远不会“打断 AI”或“被 VAD 提前 endTurn
        // 抢话”，不破坏 i13/i16 协议级断言。
        // 解决的现象：单次录音连说两句，第一句不会被第二句 partial 覆盖。
        onSpeakingEnd: () => {
          const cur = partialRef.current;
          if (!cur) return;
          const prefix = committedSttPrefixRef.current;
          const merged = prefix
            ? prefix.replace(/\s+$/u, "") + (prefix.endsWith("。") ? "" : " ") + cur
            : cur;
          committedSttPrefixRef.current = merged;
          setCommittedSttPrefix(merged);
          setPartial("");
          // #region agent log
          _dbg("InterviewPage.tsx:onSpeakingEnd", "VAD auto-commit partial to prefix", {
            partialLen: cur.length,
            newPrefixLen: merged.length,
          });
          // #endregion
        },
      });
      await cap.start();
      captureRef.current = cap;
      recordingRef.current = true;
      setRecording(true);
      setMicOn(true);
      setWaveState("listening");
      void speakingFramesRef;
    } catch (e: any) {
      setError("麦克风启动失败：" + e.message);
    }
  };

  const stopMic = () => {
    // i13 / i16 / v0.3：纯停麦，不再 endTurn。
    // AI 推进只走 [发送本轮回答] → sendAnswerText → answer_text。
    // v0.4：本次录音的最终 partial（如果还没被 final 替换）也并入
    // ``committedSttPrefix``，确保下一次开录音时不丢之前的草稿。
    // #region agent log
    _dbg("InterviewPage.tsx:stopMic", "stopMic called (v0.4 accumulating)", {
      recording: recordingRef.current,
      partialLen: partial.length,
      prefixLen: committedSttPrefixRef.current.length,
    });
    // #endregion
    recordingRef.current = false;
    captureRef.current?.stop();
    captureRef.current = null;
    setMicOn(false);
    setRecording(false);
    // 兜底：服务端 final 还没回来就已停麦的话，partial 当 commit 用。
    if (partial && committedSttPrefixRef.current.length < (textAnswer || "").length) {
      const merged = textAnswer.trim();
      committedSttPrefixRef.current = merged;
      setCommittedSttPrefix(merged);
    }
    setPartial("");
    if (waveStateRef.current === "listening") {
      setWaveState("idle");
      waveStateRef.current = "idle";
    }
  };

  // i12 / i14 — [朗读] 按钮：把目标 AI 文本气泡的全文一次性回送给后端
  // ``client_replay_tts``，由后端走与 next_question 相同的 ``play_text_stream``
  // 路径合成 + 推 ``ai_audio*`` 帧。
  const speakAiText = (text: string) => {
    const t = (text || "").trim();
    if (!t) return;
    try {
      wsRef.current?.requestTTS(t);
    } catch {
      /* noop */
    }
  };

  // 找到最新一条 AI 气泡的文本（用于全局 [朗读最新一句] 按钮）。
  const latestAiText = (() => {
    for (let i = turns.length - 1; i >= 0; i--) {
      if (turns[i].role === "interviewer") return turns[i].text || "";
    }
    return "";
  })();

  const sendTextAnswer = () => {
    const t = textAnswer.trim();
    if (!t) return;
    // #region agent log
    _dbg("InterviewPage.tsx:sendTextAnswer", "user clicked send-text", {
      hasWs: !!wsRef.current,
      textLen: t.length,
    });
    // #endregion
    wsRef.current?.sendAnswerText(t);
    // v0.5：提交本轮回答时把用户的这段话追加到右边对话历史。
    // 之前 v0.4 重构把 stt_final 的 auto-append 拆掉迁移到这里，但漏了
    // setTurns 这一步，导致用户提交后右边对话框看不到自己说的话；同时
    // score_update 事件因为找不到 candidate turn 无法落地分数更新。
    setTurns((prev) => [...prev, { role: "candidate", text: t }]);
    // v0.4：发送后重置 STT 累加 prefix；下一轮录音从空开始。
    committedSttPrefixRef.current = "";
    setCommittedSttPrefix("");
    setTextAnswer("");
    setPartial("");
    setWaveState("thinking");
  };

  const triggerEndInterview = async () => {
    wsRef.current?.endInterview();
    try {
      await apiClient.endInterview(sid, "user");
    } catch {
      /* noop */
    }
    setEnded(true);
    setEndReason("user");
  };

  if (!info) {
    return (
      <div className="text-center py-20 text-muted-foreground">
        加载面试中...
      </div>
    );
  }

  return (
    <div className="grid lg:grid-cols-[420px_1fr] gap-6">
      {/* 进入面试弹窗：先开麦再开始，规避“先 TTS 后 STT 异常”问题 */}
      <AlertDialog open={!ended && !micPrimed}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>准备开始面试</AlertDialogTitle>
            <AlertDialogDescription>
              为保证语音识别稳定工作，请先授权并打开麦克风，再正式进入面试环节。
              <br />
              如果先播放 AI 语音再开麦，可能会导致 STT 无法正常识别。
            </AlertDialogDescription>
          </AlertDialogHeader>
          {error && (
            <div className="text-xs text-destructive">{error}</div>
          )}
          <AlertDialogFooter>
            <Button
              variant="outline"
              onClick={() => nav(-1)}
              disabled={primingMic}
              data-testid="btn-prime-mic-cancel"
            >
              返回
            </Button>
            <Button
              onClick={async () => {
                setPrimingMic(true);
                try {
                  await startMic();
                  // startMic 内部失败会 setError，captureRef.current 仍为 null
                  if (captureRef.current) {
                    setMicPrimed(true);
                  }
                } finally {
                  setPrimingMic(false);
                }
              }}
              disabled={primingMic}
              data-testid="btn-prime-mic-confirm"
            >
              <Mic className="h-4 w-4 mr-2" />
              {primingMic ? "正在开麦..." : "开启麦克风并开始面试"}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      {/* 左：状态 + 控制 */}
      <div className="space-y-4">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center justify-between">
              <span>{TYPE_LABEL[info.interview_type] ?? "面试"}</span>
              <Badge variant="secondary">{info.eval_mode === "realtime" ? "实时评价" : "整体评价"}</Badge>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="text-sm text-muted-foreground line-clamp-2">
              <span className="font-medium text-foreground">岗位：</span>
              {info.job_title}
            </div>
            <div className="flex items-center justify-center">
              <WaveAnimation state={waveState} />
            </div>
            <div className="flex items-center justify-center gap-3 text-sm">
              <Badge variant="outline" className="text-base px-3 py-1">
                当前得分 <span className="ml-2 font-bold">{score}</span>
              </Badge>
              {scoreDelta !== 0 && (
                <Badge
                  variant={scoreDelta > 0 ? "success" : "destructive"}
                  className="text-base px-3 py-1"
                >
                  {scoreDelta > 0 ? `+${scoreDelta}` : scoreDelta}
                </Badge>
              )}
            </div>
            {info.impression_breakdown?.status === "pending" && (
              <div className="rounded-md border border-dashed border-muted-foreground/30 bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
                <div className="flex items-center gap-2 font-medium text-foreground/80">
                  <span className="inline-block h-2 w-2 rounded-full bg-amber-500 animate-pulse" />
                  印象分计算中
                </div>
                <div className="mt-1">
                  当前显示为暂定起始分，正基于简历 / 岗位匹配度后台测算（约 3-5 秒）。
                </div>
              </div>
            )}
            {info.impression_breakdown?.status === "error" && (
              <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
                印象分计算失败：{info.impression_breakdown?.reason || "未知原因"}
              </div>
            )}
            <div className="flex flex-col gap-2">
              {!ended && (
                <>
                  {micOn ? (
                    <Button
                      onClick={stopMic}
                      variant="outline"
                      data-testid="btn-record-stop"
                    >
                      <MicOff className="h-4 w-4 mr-2" />
                      结束录音
                    </Button>
                  ) : (
                    <Button onClick={startMic} data-testid="btn-record-start">
                      <Mic className="h-4 w-4 mr-2" />
                      开始录音
                    </Button>
                  )}
                  <Button
                    onClick={() => speakAiText(latestAiText)}
                    variant="secondary"
                    data-testid="btn-speak-ai-text-global"
                    disabled={!latestAiText}
                    title="朗读最新一段 AI 文本（手动触发 TTS）"
                  >
                    <Volume2 className="h-4 w-4 mr-2" />
                    朗读 AI 最新一句
                  </Button>
                  <AlertDialog>
                    <AlertDialogTrigger asChild>
                      <Button variant="destructive" data-testid="btn-end-interview">
                        <StopCircle className="h-4 w-4 mr-2" />
                        结束面试
                      </Button>
                    </AlertDialogTrigger>
                    <AlertDialogContent>
                      <AlertDialogHeader>
                        <AlertDialogTitle>确认结束面试？</AlertDialogTitle>
                        <AlertDialogDescription>
                          结束后将跳转到复盘页面，已记录的对话会用于生成报告。
                        </AlertDialogDescription>
                      </AlertDialogHeader>
                      <AlertDialogFooter>
                        <AlertDialogCancel>再考虑一下</AlertDialogCancel>
                        <AlertDialogAction
                          onClick={triggerEndInterview}
                          data-testid="btn-end-confirm"
                        >
                          确认结束
                        </AlertDialogAction>
                      </AlertDialogFooter>
                    </AlertDialogContent>
                  </AlertDialog>
                </>
              )}
              {ended && (
                <Button onClick={() => nav(`/report/${sid}`)} data-testid="btn-go-report">
                  <CheckCircle2 className="h-4 w-4 mr-2" />
                  查看复盘报告
                </Button>
              )}
            </div>
            {error && (
              <div className="text-xs text-destructive">{error}</div>
            )}
            {ended && endReason && (
              <div className="text-xs text-muted-foreground">
                结束原因：{labelOfEnd(endReason)}
              </div>
            )}
          </CardContent>
        </Card>

        {!ended && (
          <Card>
            <CardHeader>
              <CardTitle className="text-base">文本作答（无麦时使用）</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              <Textarea
                value={textAnswer}
                onChange={(e) => {
                  const v = e.target.value;
                  setTextAnswer(v);
                  // 用户手动编辑 textarea（含删除）时，必须把 prefix 同步到
                  // textarea 的实际内容，并清掉还在识别中的 partial，否则
                  // 下一次 stt_partial 到达会用旧 prefix 重新拼接，把用户
                  // 刚删的内容又「冒回来」。
                  committedSttPrefixRef.current = v;
                  setCommittedSttPrefix(v);
                  setPartial("");
                }}
                placeholder="如果不便使用麦克风，可以直接输入回答..."
                rows={4}
                data-testid="input-text-answer"
              />
              <Button
                onClick={sendTextAnswer}
                className="w-full"
                disabled={!textAnswer.trim()}
                data-testid="btn-send-text"
              >
                <Send className="h-4 w-4 mr-2" />
                提交本轮回答
              </Button>
            </CardContent>
          </Card>
        )}
      </div>

      {/* 右：对话流 */}
      <Card className="flex flex-col" style={{ minHeight: 600 }}>
        <CardHeader>
          <CardTitle className="text-base">对话记录</CardTitle>
        </CardHeader>
        <CardContent
          className="flex-1 overflow-y-auto space-y-3 max-h-[70vh]"
          id="dialog-pane"
          ref={dialogPaneRef}
        >
          {turns.length === 0 && !partial && (
            <div className="text-center text-muted-foreground py-12">
              <Play className="h-6 w-6 mx-auto mb-2" />
              面试官即将开始提问，请准备好...
            </div>
          )}
          {turns.map((t, i) => (
            <DialogBubble key={i} t={t} onSpeak={speakAiText} />
          ))}
          {/* 识别中的 partial 文字只在左下 textarea 中显示，这里不再渲染
              「[识别中] xxx」气泡，避免与已提交 turn 视觉混淆。 */}
        </CardContent>
      </Card>

      <audio ref={audioElRef} hidden controls={false} preload="auto" />
    </div>
  );
}

function DialogBubble({
  t,
  onSpeak,
}: {
  t: DialogTurn;
  onSpeak?: (text: string) => void;
}) {
  const isAI = t.role === "interviewer";
  const e = t.evaluator as
    | { strengths?: string; weaknesses?: string; reference?: string }
    | undefined;
  return (
    <div className={`flex ${isAI ? "justify-start" : "justify-end"}`}>
      <div
        className={`max-w-[82%] rounded-lg px-3 py-2 text-sm border ${
          isAI
            ? "bg-card border-border"
            : "bg-accent/10 border-accent/30"
        }`}
      >
        <div className="flex items-center gap-2 mb-1">
          <Badge variant={isAI ? "secondary" : "default"} className="text-[10px]">
            {isAI ? "面试官" : "我"}
          </Badge>
          {t.strategy && t.strategy !== "opening" && (
            <Badge variant="outline" className="text-[10px]">
              {labelOfStrategy(t.strategy)}
            </Badge>
          )}
          {!isAI && typeof t.delta === "number" && (
            <Badge
              variant={t.delta > 0 ? "success" : t.delta < 0 ? "destructive" : "outline"}
              className="text-[10px]"
            >
              {t.delta > 0 ? `+${t.delta}` : t.delta} → {t.total}
            </Badge>
          )}
          {/* v0.4：每条 AI 气泡右上角加 [朗读] 按钮，触发该条文本 TTS。
              全局按钮 [btn-speak-ai-text-global] 仍朗读最新一条作为快捷。 */}
          {isAI && onSpeak && (t.text || "").trim() && (
            <button
              type="button"
              onClick={() => onSpeak(t.text)}
              className="ml-auto inline-flex items-center justify-center rounded-md p-1 text-muted-foreground hover:text-foreground hover:bg-accent/40 transition-colors"
              title="朗读这一段（手动触发 TTS）"
              data-testid="btn-speak-bubble"
            >
              <Volume2 className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
        <div className="whitespace-pre-wrap leading-relaxed">{t.text}</div>
        {!isAI && e && (e.strengths || e.weaknesses) && (
          <div className="mt-2 text-xs space-y-1 border-t pt-2 text-muted-foreground">
            {e.strengths && (
              <div>
                <span className="text-emerald-600">优点：</span>
                {e.strengths}
              </div>
            )}
            {e.weaknesses && (
              <div>
                <span className="text-amber-600">不足：</span>
                {e.weaknesses}
              </div>
            )}
            {e.reference && (
              <div>
                <span className="text-blue-600">参考：</span>
                {e.reference}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function labelOfStrategy(s: string): string {
  switch (s) {
    case "breadth":
      return "广度";
    case "depth":
      return "深度追问";
    case "wrap_up":
      return "收尾";
    case "interrupt":
      return "打断";
    default:
      return s;
  }
}

function labelOfEnd(s: string): string {
  switch (s) {
    case "user":
      return "用户主动结束";
    case "score_threshold":
      return "AI 触发熔断（得分过低）";
    case "complete":
      return "正常结束";
    default:
      return s;
  }
}
