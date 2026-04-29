# 端到端回归脚本：先启动后端 + 前端（如果尚未启动），然后人工/Playwright MCP 走通 playbook
# 真实凭据请放在 .env.local；E2E 脚本不会读取，仅由前端表单填入。

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Write-Host "Make sure dev servers are running. If not, run scripts/dev.ps1 first." -ForegroundColor Yellow
Write-Host "Then follow tests/e2e/playbook.md step-by-step using Playwright MCP." -ForegroundColor Cyan

Start-Process "http://127.0.0.1:5173"
