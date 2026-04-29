# QiInterview 一键开发启动脚本（Windows PowerShell）
# 在两个新 PowerShell 窗口分别启动 backend 与 frontend。

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Write-Host "======================================" -ForegroundColor Cyan
Write-Host " QiInterview Dev Launcher" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
Write-Host "Workspace: $root"

# ---- 检测 Python 环境 ----
$pythonCmd = "python"
if (Get-Command "py" -ErrorAction SilentlyContinue) {
    $pythonCmd = "py -3"
}

# ---- 启动后端 ----
$backendDir = Join-Path $root "backend"
Write-Host "`n[1/2] Starting backend at $backendDir"
$venv = Join-Path $backendDir ".venv"
if (-not (Test-Path $venv)) {
    Write-Host "Creating Python venv ..." -ForegroundColor Yellow
    & cmd /c "cd /d `"$backendDir`" && $pythonCmd -m venv .venv"
}
$activate = Join-Path $venv "Scripts\Activate.ps1"

# 避免同一 PowerShell 会话里曾跑过 pytest 时遗留的 QI_LLM_MOCK=1 污染子进程
$backendCmd = "cd `"$backendDir`"; Remove-Item env:QI_LLM_MOCK -ErrorAction SilentlyContinue; & `"$activate`"; pip install -e . --quiet --disable-pip-version-check; python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $backendCmd

# ---- 启动前端 ----
$frontendDir = Join-Path $root "frontend"
Write-Host "[2/2] Starting frontend at $frontendDir"
$pmCmd = "npm install --no-audit --no-fund && npm run dev"
if (Get-Command "pnpm" -ErrorAction SilentlyContinue) {
    $pmCmd = "pnpm install && pnpm dev"
}
$frontendCmd = "cd `"$frontendDir`"; $pmCmd"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $frontendCmd

Write-Host "`nLaunched. Open http://127.0.0.1:5173 once Vite finishes." -ForegroundColor Green
