# QiInterview2 传输安全 Audit 备忘（P6 / Email + 传输硬化）

> 本文档由 `email-register-and-transport-safety` 计划交付，作为「上线前的
> 安全责任划分清单」与「未来回归 PR 的红线参照」。任何对下列锁定点的回退
> 都必须先回答："对应的回归测试为什么不再相关？"

## 1. 总览：两栏责任划分

QiInterview2 的传输面分两层：**应用层（本仓库代码 + 单测/E2E 锁定）**
与 **运维层（反代/操作系统/CA 链，超出本仓库范围）**。下面分两栏列：

### 1.1 已防（应用层 — 本仓库 + 测试守门）

| 风险面 | 锁定方式 | 代码点 | 守门测试 |
| --- | --- | --- | --- |
| 注册 / 登录密码明文上行 | RSA-OAEP(SHA-256) 客户端加密 → 后端 `decrypt_password` 解；私钥单例不入库 | `frontend/src/store/auth.ts` `encryptPassword`；`backend/app/api/auth.py` `_decrypt_or_validate_password`；`backend/app/core/rsa_keys.py` | E2E `TestPhase5LoginPayloadEncrypted::test_b1`、`TestPhase5RegisterPayloadEncrypted::test_b2` |
| Session cookie 在 prod 走 HTTPS 不带 `Secure` / 跨站可读 | 单一收口：`_set_session_cookie` 读 `Settings.cookie_secure` → `Secure; SameSite=Strict; HttpOnly` | `backend/app/api/auth.py:167-184` | 后端 `tests/backend/test_transport_safety.py::test_session_cookie_respects_secure_flag`；E2E `TestPhase5CookieSecureProd::test_b4` |
| prod 部署忘开 cookie/host/cors → 启动期才发现问题 | `_enforce_prod_safety` 在 startup 抛 `RuntimeError`，覆盖 `cookie_secure / allowed_hosts / cors_origins` 三件事 | `backend/app/main.py:156` `_enforce_prod_safety`；`backend/app/main.py:196` 调用点 | `tests/backend/test_transport_safety.py::test_prod_app_env_requires_*` |
| 缺 HSTS / 缺基础 CSP / 跨域帧嵌入 / 嗅探 MIME | `SecurityHeadersMiddleware` 始终发 `X-Content-Type-Options / Referrer-Policy / X-Frame-Options`；`prod` 追加 `Strict-Transport-Security`、最小 CSP | `backend/app/main.py:109` `SecurityHeadersMiddleware`；`backend/app/main.py:230` 注册 | `tests/backend/test_transport_safety.py::test_security_headers_*` |
| Host 头注入 / open-host | `TrustedHostMiddleware`，prod 必须显式 `ALLOWED_HOSTS` | `backend/app/main.py:208` | 由 `_enforce_prod_safety` 配套校验 |
| OTP / reset token 落库即明文 | 全部只落 `sha256` —— `_hash_token` 是单一收口；DB 中只有 `code_hash / token_hash` 两列，无任何 plaintext 列 | `backend/app/api/auth.py:231` `_hash_token`；`backend/app/models/user.py` `EmailVerification` | `tests/backend/test_auth_email.py::test_*_stored_as_hash` |
| OTP / reset token 串号 / 重放 / 越期 | `consumed_at` 单调；TTL 由 `mail_otp_ttl_minutes / mail_reset_ttl_minutes` 控；密码重置成功后 `DELETE FROM auth_session WHERE user_id=X` 强制全端登出 | `backend/app/api/auth.py` `register/verify` & `password-reset/confirm` 全流程 | 后端 `test_register_verify_*`、`test_password_reset_*`；E2E `TestAuthEmail::test_f2_reset_token_consumed_once` |
| 邮箱 enumeration（账号是否存在被探） | `register/start` 与 `password-reset/start` 一律返回 200；判定逻辑在限流之前完成，避免 timing 旁路 | `backend/app/api/auth.py` 两个 start 端点 | `tests/backend/test_auth_email.py::test_register_start_no_enumeration`、`test_password_reset_unknown_email_still_200` |
| 同邮箱大量发信 / 邮件轰炸 | `mail_send_min_interval_sec`（默认 60s）+ pending 上限 | `backend/app/api/auth.py` start 端点 | `test_register_start_rate_limit` |
| 邮件凭据落仓库 / 落生产日志 | `MailSender` 抽象 + `ConsoleMailSender`（dev/E2E 写 `backend/data/dev_mail/*.json`，已入 `.gitignore`）+ `SmtpMailSender`（prod 凭据仅来自 env） | `backend/app/services/mail.py`；`get_mail_sender` factory 读 `Settings.mail_backend` | `test_auth_email.py` 全部用例都靠 `_MailBox` fixture 读 dev_mail；E2E `_dev_mail_pop_*` 全部走文件桶 |
| OTP / reset token / 密码 进结构化日志 | `_qidbg` 收口；只允许 `email_hash16 / purpose / ok / elapsed_ms / has_*`；任何带原文/完整密文的字段都走专门的拦截器 | `backend/app/api/auth.py:194` `_qidbg`、`_email_hash16` | `tests/backend/test_transport_safety.py::test_qidbg_logs_never_contain_secret_originals` |
| OTP / reset token 泄到浏览器 console / DOM / API body | E2E 全量回放浏览器侧通道做断言 | — | `tests/e2e/test_e2e_qiinterview.py::TestAuthEmail::test_a5_otp_never_leaks_to_browser` |
| `tests/diag/*.py` 的真实 LLM / 语音 Key 硬编码 | 全量改成 `os.environ[...]` + `sys.exit("env not set; refusing to embed plaintext key")` | `tests/diag/tts_test_vol.py` / `stt_test_vol.py` / `probe_concurrent_get.py` / `probe_turn_writes.py` / `probe_writer_vs_reader.py` | `tests/backend/test_transport_safety.py::test_diag_scripts_have_no_hardcoded_uuid_keys` |
| 前端 zustand 持久化把 API Key 落 localStorage | `partialize` 显式排除 `llmKey / volcVoiceKey / dashscopeKey / voice*Token` | `frontend/src/store/credentials.ts` partialize | E2E `TestSecurity::test_sec6_keys_not_persisted_in_browser_storage` |
| API Key 拼到 URL（GET query / WS query） | REST 走 `X-LLM-Key / X-Volc-Voice-Key` Header；WS 走「先连后发 auth 帧」 | `backend/app/api/voice_ws.py` `voice_creds_from_query` 移除查询取值；`frontend/src/lib/ws.ts` 首帧 auth | E2E `TestVoice::test_v21_volc_voice_key_goes_through_ws_auth_not_url`、`TestSecurity::test_sec4 / test_sec7` |

### 1.2 仅依赖运维侧 TLS（应用层无能为力）

| 风险面 | 仅 TLS 能挡 | 部署清单要求 |
| --- | --- | --- |
| 公网 MITM 拦截 / 改写 HTTPS 流量 | TLS 握手 + 证书链 | 反代必须开 TLS（建议 TLS1.2+，禁用弱套件）；签发链路用受信 CA |
| `X-LLM-Key / X-Volc-Voice-Key` 在 Header 上的明文 | 同上（这两把 Key 不可能再做端到端加密：上游火山 / 阿里云本身就要明文 Key 鉴权） | 同上；务必把出站火山 / 阿里上游也限制为 TLS（默认就是 HTTPS） |
| WebSocket 首帧 auth payload 的明文 token | TLS（即 `wss://`） | 反代到后端用 `wss://`；前端 `vite.config.ts` 的 `target` 在生产改成 `https://...`，而非现在的 `http://127.0.0.1:8001` |
| 客户端 → 反代之间的 HTTPS 终止位置 | 由部署决定 | `Set-Cookie: Secure` 仅在 HTTPS 链路上才被浏览器尊重；`COOKIE_SECURE=true` 必须配真 TLS |
| Email / SMTP 上行 | SMTP STARTTLS / SMTPS | `SMTP_USE_STARTTLS=True`（默认）；选 587/STARTTLS 或 465/SMTPS；`mail_from` 为受信域 |

> 本节的所有条目，应用层都已经把"非 TLS 也能泄"的具体路径扫干净
> （cookie / dev_mail 文件 / 日志 / diag 脚本 / 浏览器 storage / URL）。
> 真要 prod 上线，**TLS 仍是不可替代的最后一公里**。

## 2. 五类 Lockdown 点（plan 验收口径）

下面五类是本次 plan 显式承诺、也是后续 PR 不准回退的硬约束：

1. **Cookie**：`backend/app/api/auth.py` `_set_session_cookie` 是仓库**唯一**
   写 `qi_session` cookie 的位置；`Secure / SameSite` 全部由 `Settings.cookie_secure`
   控；prod 启动期硬校验由 `_enforce_prod_safety`（`backend/app/main.py:156`）兜底。
   守门：`tests/backend/test_transport_safety.py::test_session_cookie_respects_secure_flag`、
   `test_prod_app_env_requires_cookie_secure`、`tests/e2e/test_phase5_security_voice.py::TestPhase5CookieSecureProd::test_b4_session_cookie_secure_when_prod_env`。

2. **Mail**：`backend/app/services/mail.py` 是仓库**唯一**发邮件的位置；
   `MailSender` Protocol → `ConsoleMailSender` (dev/E2E，写 `backend/data/dev_mail/`，
   已入 `.gitignore`) / `SmtpMailSender`（prod，凭据只从 env 来；`SmtpMailSender.send`
   做 `asyncio.to_thread` 包裹的 `smtplib`）。`get_mail_sender()` factory 是单例选择器；
   `set_mail_sender()` 仅供测试 fixture 用。守门：`test_auth_email.py::test_register_*` / `test_password_reset_*`
   全部基于 `_MailBox` fixture 真实读 dev_mail，绝不 mock 业务函数。

3. **OTP / Reset Token 在 DB**：`backend/app/api/auth.py` `_hash_token` 是仓库
   **唯一**把 OTP / token 落 DB 的入口，且只落 sha256；
   `EmailVerification` 表（`backend/app/models/user.py`）只有 `code_hash / token_hash`，
   不存在任何 plaintext 列。守门：`test_auth_email.py::test_register_otp_stored_as_hash_only`
   / `test_reset_token_stored_as_hash_only`，断言 raw OTP / token 在 DB 全表扫描里**完全找不到**。

4. **`_qidbg` 结构化日志**：`backend/app/api/auth.py:194` `_qidbg` 是 P6 邮箱链路的
   **唯一**埋点出口；约定的 data 字段集是 `email_hash16 / purpose / ok / elapsed_ms / has_*`；
   任何带原文 / 完整密文 / 6 位 OTP / 43 字符 reset token 的字段都不进 `data`。
   守门：`tests/backend/test_transport_safety.py::test_qidbg_logs_never_contain_secret_originals`
   会跑一遍 register / login / reset 全流程，再扫 `.cursor/debug-714cc8.log`，
   命中 OTP（带上下文 JSON 字段）/ 密文 / 密码原文 → 红。

5. **`tests/diag/*.py` 不留硬编码 Key**：所有 diag 脚本统一改 `os.environ[...]` +
   `sys.exit(...)`，提交前 lint 测试 `tests/backend/test_transport_safety.py::test_diag_scripts_have_no_hardcoded_uuid_keys`
   做"全量字面 UUID 扫描"，命中 `aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa` 形态字面量直接红，
   阻止"提交前忘了删 key"这种最常见的回归。

## 3. 部署清单（与 README 「9. 生产部署清单」对齐）

下面 6 条是 prod 上线**最小必备**集，缺任意一条 `_enforce_prod_safety` 都会让 backend 拒绝起来：

```
APP_ENV=prod
COOKIE_SECURE=true
ALLOWED_HOSTS=["your.domain","another.domain"]
CORS_ORIGINS=["https://your.frontend.domain"]
MAIL_BACKEND=smtp
SMTP_HOST=...  SMTP_PORT=587  SMTP_USER=...  SMTP_PASSWORD=...  SMTP_USE_STARTTLS=true  MAIL_FROM=no-reply@your.domain
FRONTEND_BASE_URL=https://your.frontend.domain
```

外加运维侧：反代必须 TLS 终止；`vite.config.ts` 的 dev `target` 仅供本地，prod
直接走反代；SMTP 出站强制 STARTTLS（默认开）。

## 4. 已知"应用层无法独立挡"的剩余风险

- **上游火山 / 阿里云语音 Key 上行的明文性**：调上游必然要明文 Key，无法做端到端加密。
  缓解：把 Key 限制为最小权限、定期轮换、加 IP 白名单（如果上游支持）。
- **凭据泄露后的 blast radius**：当前没有"撤销单 LLM Key" 的中心化吊销表。
  下一步演进可加一张 `revoked_keys` 表 + 出站前查询。本期暂未排上日程。
- **客户端浏览器扩展能读 DOM**：`SecurityHeadersMiddleware` 已加 `X-Frame-Options=DENY`
  挡 iframe，但浏览器扩展读 DOM 是 OS 级权限，应用层无能为力。

> 任何后续 PR 若要回退第 2 节的五类锁定点，必须同时回答：
>
> 1. 对应守门测试为什么允许变绿/被删？
> 2. 等价的安全保证在哪里替代？
>
> 否则评审一律 reject。
