# WorkBuddy / Windows 业务机：依赖安装 + 环境校验（无 Git / 无 bash 时）
# Usage: powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_windows.ps1

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$Root = Split-Path -Parent $PSScriptRoot
$Py = $env:STREAM_GEN_PYTHON
if (-not $Py) { $Py = "python" }

Set-Location $Root
Write-Host "[bootstrap_windows] workspace=$Root"

if (-not (Test-Path ".env")) {
    Write-Host "[bootstrap_windows] ERROR: missing .env (copy from .env.example)" -ForegroundColor Red
    exit 10
}

Write-Host "[bootstrap_windows] pip install -r requirements.txt"
& $Py -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "[bootstrap_windows] pip failed; try: python -m pip install --user -r requirements.txt" -ForegroundColor Yellow
}

& $Py scripts\verify_env.py --repo-root $Root
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $Py scripts\deploy_readiness.py --repo-root $Root
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "[bootstrap_windows] OK. After code/zip update, delete cache\snapshot\* then run:" -ForegroundColor Green
Write-Host "  .\scripts\query_market_facts.ps1 --sources market,news,social --summary-only --force-refresh"
Write-Host "  Get-Content cache\snapshot\markdown_summary.md -Encoding utf8 -TotalCount 15"
exit 0
