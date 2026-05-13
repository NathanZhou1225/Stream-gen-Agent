#!/bin/bash
# Stream-Gen 会话清理脚本（安全版）
# 不直接删除会话文件，统一搬运到 sessions/_session_cleanup_archive/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "=== Stream-Gen Session Cleanup (safe archive mode) ==="
"$PYTHON_BIN" "$SCRIPT_DIR/reset_min_context.py" --keep-latest 1 --compact-sessions-json
echo "=== Done ==="
