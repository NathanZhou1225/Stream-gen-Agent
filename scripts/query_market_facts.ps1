# Stream-gen · 今日行情快照（Windows / WorkBuddy）
# 自动设置 PYTHONUTF8，再调用 query_market_facts.py
# Usage:
#   .\scripts\query_market_facts.ps1 --sources market,news,social --summary-only
#   .\scripts\query_market_facts.ps1 --sources market,news,social --summary-only --force-refresh

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$Root = Split-Path -Parent $PSScriptRoot
$Py = $env:STREAM_GEN_PYTHON
if (-not $Py) { $Py = "python" }
$Script = Join-Path $Root "skills\streamy-content-gen\scripts\query_market_facts.py"

if (-not (Test-Path $Script)) {
    Write-Error "missing script: $Script"
    exit 2
}

Set-Location $Root
& $Py $Script @args
exit $LASTEXITCODE
