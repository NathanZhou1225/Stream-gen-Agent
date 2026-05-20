# 拉取并展示今日信源快照 markdown（UTF-8，Agent 原样粘贴 stdout）
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$Root = Split-Path -Parent $PSScriptRoot
$Py = $env:STREAM_GEN_PYTHON
if (-not $Py) { $Py = "python" }
$Script = Join-Path $Root "skills\streamy-content-gen\scripts\present_today_snapshot.py"

Set-Location $Root
& $Py $Script @args
exit $LASTEXITCODE
