#!/usr/bin/env bash
# 拉取并展示今日信源快照 markdown（UTF-8，Agent 原样粘贴 stdout）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${STREAM_GEN_PYTHON:-python3}"
SCRIPT="$ROOT/skills/streamy-content-gen/scripts/present_today_snapshot.py"

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

cd "$ROOT"
exec "$PY" "$SCRIPT" "$@"
