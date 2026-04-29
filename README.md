# QiInterview · 智能模拟面试系统

> 基于火山方舟（豆包/DeepSeek/Qwen/GLM 经 OpenAI SDK）+ 火山引擎语音（TTS/STT WebSocket V3）
> 端到端模拟真实大厂面试官：动态追问 / 评分 / 熔断 / 流式语音 / 复盘报告。

---

## 1. 架构总览

```mermaid
flowchart LR
  subgraph Browser
    UI[React + Tailwind + shadcn]
    AW[AudioWorklet 16k PCM]
    MS[MediaSource 播放 mp3]
  end
  UI <-- WebSocket --> WS[/FastAPI /ws/interview/_{_sid_}_/]
  UI <-- REST --> API[/FastAPI /api/.../]
  WS --> ENG[Interviewer Engine]
  ENG --> LLM[OpenAI SDK\n（doubao/deepseek/qwen/glm）]
  ENG --> SCO[Scoring + 熔断]
  WS --> TTS[Volc TTS V3 双向流]
  WS --> STT[Volc ASR bigmodel 流式]
  API --> JOBS[(SQLite\n岗位库)]
  REF[APScheduler] --> CRAW[腾讯/字节/阿里 真实接口]
  CRAW --> JOBS
  API --> DB[(SQLite\n面试记录)]
```

### 数据流

1. **创建面试**：前端把 `interview_type / job / resume` POST 到 `/api/interviews`，后端调用 LLM 算"印象分"，写入 SQLite。
2. **面试中**：浏览器 `AudioWorklet` 把麦克风重采样到 16k PCM Int16，base64 后通过 WebSocket 上行；后端把音频喂给火山 ASR 流式得到文本，再交给 `InterviewerEngine` 决定追问策略 → 调用 LLM → 调用火山 TTS 双向流 → 把 mp3 chunk 流回浏览器，`MediaSource` 拼接低延迟播放。
3. **打断**：客户端本地 RMS VAD 检测到用户开口 → `cancel()` 当前 MediaSource + 发 `user_interrupt` → 服务端取消进行中的 TTS 任务。
4. **复盘**：结束后调 `/api/reports/{sid}` 由 LLM 生成总结/亮点/不足/建议；前端用 `recharts` 画分数曲线，逐轮可展开看参考答案。

---

## 2. 目录结构

```
QiInterview2/
├── backend/                 # FastAPI + SQLAlchemy + Alembic + Jinja2 + APScheduler
│   ├── app/
│   │   ├── api/             # REST + WebSocket
│   │   ├── core/            # 凭据透传、音色路由
│   │   ├── db/              # async engine / session
│   │   ├── models/          # InterviewSession / Turn / Report / JobPost
│   │   ├── prompts/         # *.j2 提示词模板
│   │   ├── schemas/         # Pydantic v2 DTO
│   │   └── services/        # llm / tts / stt / interviewer / scoring / report / jobs
│   ├── alembic/
│   └── pyproject.toml
├── frontend/                # Vite + React + TS + Tailwind + 自带 shadcn 风格组件
│   └── src/
│       ├── pages/           # Setup / Interview / Report / History
│       ├── components/      # AppShell / WaveAnimation / JobPicker / ScoreChart / ui/
│       ├── lib/             # api / audioCapture / audioPlayer / ws
│       └── store/           # zustand 持久化设置
├── scripts/
│   ├── dev.ps1              # 一键起后端+前端
│   └── e2e.ps1              # 提示并打开浏览器跑 playbook
├── tests/
│   ├── backend/             # pytest
│   └── e2e/playbook.md      # Playwright MCP 步骤
├── README.md
└── .env.example
```

---

## 3. 准备凭据

| 名称 | 获取入口 |
|---|---|
| `ARK_API_KEY` | 火山方舟控制台 → API Key 管理 https://console.volcengine.com/ark |
| `VOLC_AUDIO_APP_ID` | 火山引擎语音技术 → 应用列表 https://console.volcengine.com/speech/app |
| `VOLC_AUDIO_ACCESS_TOKEN` | 同上 → 应用详情 → Access Token |
| `VOLC_TTS_RESOURCE_ID` | 默认 `volc.service_type.10029`（大模型语音合成） |
| `VOLC_ASR_RESOURCE_ID` | 默认 `volc.bigasr.sauc.duration` |

> 凭据不上传第三方；前端浏览器 `localStorage` 缓存 + 后端透传到火山接口。

复制 `.env.example` 为 `.env.local` 即可。亦可不写 env，开发时直接在 SetupPage 上填入。

---

## 4. 本地启动

```powershell
# 一键启动（会另开两个 PowerShell 窗口）
./scripts/dev.ps1
```

或手动：

```powershell
# 后端
cd backend
python -m venv .venv
. .venv/Scripts/Activate.ps1
pip install -e .
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# 前端
cd frontend
npm install
npm run dev
```

打开 http://127.0.0.1:5173 ：

1. 在左上"凭据配置"填入 LLM Key + 语音 AppID/Token；
2. 选择面试类型（一面/二面/综合/HR）+ 评价模式；
3. 在"大厂岗位库"标签里选一条岗位（首次会触发后台抓取，等几秒后点搜索）；
4. 点击"开始模拟面试"。

---

## 5. WebSocket 协议（面试过程）

```jsonc
// Client → Server
{"type":"start"}                                     // 触发开场白
{"type":"audio_chunk","pcm_base64":"..."}            // 16k mono Int16
{"type":"answer_text","text":"..."}                  // 文本作答（无麦时）
{"type":"end_turn"}                                  // 一段静默后自动触发
{"type":"user_interrupt"}                            // RMS 超阈值时
{"type":"end_interview"}                             // 用户结束

// Server → Client
{"type":"ai_thinking"}
{"type":"ai_text","text":"...","strategy":"..."}
{"type":"ai_audio","mime":"audio/mp3","chunk_b64":"..."}
{"type":"ai_audio_end","interrupted":false}
{"type":"stt_partial","text":"..."}
{"type":"stt_final","text":"...","turn_idx":5}
{"type":"score_update","turn_idx":5,"delta":-3,"total":71,"evaluator":{...}}
{"type":"ai_interrupt","reason":"off_topic"}
{"type":"interview_end","reason":"user|score_threshold|complete"}
{"type":"error","message":"..."}
```

---

## 6. 设计决策

- **不硬编码任何岗位 / 评分阈值**：岗位实时从腾讯/字节/阿里官网接口抓取，TTL 6h；阈值与音色映射见 `app/config.py`。
- **凭据 0 入库**：API Key 走前端 → HTTP Header / WS Query → 后端临时使用，不进 DB。
- **打断时延**：服务端取消任务 + 客户端 `MediaSource.endOfStream("decode")`，可在 1s 内静音。
- **STT 准确率**：火山 bigmodel 已是业内 top；可在 `services/stt.py` 注入 hot_words（JD 关键词）。
- **多模型支持**：通过 OpenAI SDK 的 `base_url` 切换，4 家服务商通用同一份代码。

---

## 7. 测试

```powershell
# 后端单测
cd backend
pip install -e .[dev]
pytest -q ../tests/backend

# 端到端 - 见 tests/e2e/playbook.md
./scripts/e2e.ps1
```

E2E 由 Playwright MCP 驱动，**严禁 mock**：使用真实 LLM/语音 key（可来自 `.env.local`），并使用 Chromium `--use-file-for-fake-audio-capture` 注入预录 wav 模拟麦克风。详见 `tests/e2e/playbook.md`。

---

## 8. 常见问题

- **"缺少 LLM API Key"**：在 SetupPage 左上角"凭据配置"填入。
- **岗位库为空**：等待几秒后点击"刷新"按钮，后台抓取需要 5-15 秒；亦可改用"自定义岗位"标签手动输入 JD。
- **听不到 AI 声音**：F12 控制台搜 `appendBuffer`；常见原因是浏览器自动播放受限，第一次需要点击页面（启用麦克风按钮已经触发 user gesture）。
- **STT 识别为空**：检查麦克风权限；在控制台运行 `navigator.mediaDevices.getUserMedia({audio:true})` 确认设备可用。

---

## 9. 生产部署清单（P6 / 传输安全硬化）

把项目从 dev 推到公网时，**以下任一项缺失** `create_app` 启动期都会
直接 `RuntimeError`，不需要靠人脑记：

| `.env.local` | 期望值 | 后果（不达标） |
| --- | --- | --- |
| `APP_ENV` | `prod` | 不切到 prod 不触发后续硬校验 |
| `COOKIE_SECURE` | `true` | cookie 不带 `Secure`，HTTP 网段被劫持即丢号 |
| `ALLOWED_HOSTS` | 显式域名（不能是 `*`） | Host header injection 可让密码重置链接拼到攻击者域 |
| `CORS_ORIGINS` | 显式前端 origin（不能含 `*`） | 配合 `credentials=true` 等于把任意第三方页都允许带 cookie |
| `MAIL_BACKEND` | `smtp` | console 落本地文件，公网用户收不到验证码 |
| `SMTP_*` / `MAIL_FROM` | 真实 SMTP 凭据 | 同上；同时 `SMTP_SECURITY=starttls` 或 `ssl`，**禁用 `none`** |
| `FRONTEND_BASE_URL` | 公网前端 https URL | 重置链接里出现 localhost，邮件等于废纸 |

部署侧另需配齐反向代理（nginx / caddy）启用 TLS（建议 `Let's Encrypt`），
`Strict-Transport-Security` 与 `Content-Security-Policy` 由 `SecurityHeadersMiddleware`
在 prod 模式自动追加，无需在反代再写一份。

详见 [`docs/SECURITY.md`](docs/SECURITY.md) 的"已防 / 仅依赖运维侧 TLS"两栏。

---

Powered by Volcengine Doubao &amp; Speech · v0.1
