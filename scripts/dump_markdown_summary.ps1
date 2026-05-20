# 输出 cache/snapshot/markdown_summary.md（UTF-8 纯文本，WorkBuddy 展示用）
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$Root = Split-Path -Parent $PSScriptRoot
$Py = $env:STREAM_GEN_PYTHON
if (-not $Py) { $Py = "python" }
$Script = Join-Path $Root "skills\streamy-content-gen\scripts\dump_markdown_summary.py"

Set-Location $Root
& $Py $Script @args
exit $LASTEXITCODE
