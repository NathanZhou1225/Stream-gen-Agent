#!/usr/bin/env bash
# Stream-gen 工作区 bootstrap：可单独作为 Git 仓库根；也可嵌在 OpenClaw 整仓的 workspace-stream-gen/ 下。
# 问财 zip：仅在完整 OpenClaw 仓时见 ../../scripts/iwencai_skillhub_download_and_install.sh（本仓若不含可忽略）。
set -euo pipefail

WS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$WS_ROOT"

PARENT="$(cd "$WS_ROOT/.." && pwd)"
echo "[bootstrap] workspace_root=$WS_ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 is required." >&2
  exit 1
fi

REQ="$WS_ROOT/requirements.txt"
if [[ -f "$REQ" ]]; then
  echo "[bootstrap] pip install -r requirements.txt"
  if ! python3 -m pip install -r "$REQ"; then
    echo "[bootstrap] pip failed (e.g. PEP 668); retry with --break-system-packages (override: set STREAM_GEN_PIP_NO_BREAK=1 and use a venv)." >&2
    if [[ "${STREAM_GEN_PIP_NO_BREAK:-}" == "1" ]]; then
      echo "[bootstrap] STREAM_GEN_PIP_NO_BREAK=1 set, aborting pip." >&2
      exit 1
    fi
    python3 -m pip install --break-system-packages -r "$REQ"
  fi
else
  echo "Warning: requirements.txt missing, skip pip install." >&2
fi

has_env=0
[[ -f "$WS_ROOT/.env" ]] && has_env=1
if [[ -f "$PARENT/openclaw.json" ]] && [[ -f "$PARENT/.env" ]]; then
  has_env=1
fi

if [[ "$has_env" -eq 0 ]]; then
  echo "======================================================"
  echo "[AGENT_INSTRUCTION_REQUIRED]"
  echo "phase=missing_dotenv"
  echo "next_steps=read_dotenv_example_and_ask_user"
  echo "workspace_root=$WS_ROOT"
  echo "hint=Read .env.example. Router + Rewriter default ON. P1: FINANCE_CLOUD_API_BASE_URL + FINANCE_CLOUD_API_KEY (or STREAM_GEN_SKIP_P1_READINESS=1). Live legacy: --live-fetch only. Feishu optional. Do not echo secrets."
  echo "======================================================"
  exit 10
fi

echo "[bootstrap] running scripts/verify_env.py"
if ! python3 "$WS_ROOT/scripts/verify_env.py" --repo-root "$WS_ROOT"; then
  echo "[bootstrap] verify_env failed (exit 1)." >&2
  exit 1
fi

echo "[bootstrap] running scripts/deploy_readiness.py (P1 信源增强项)"
if ! python3 "$WS_ROOT/scripts/deploy_readiness.py" --repo-root "$WS_ROOT"; then
  echo "[bootstrap] deploy_readiness failed (P1 缺口未补或需 STREAM_GEN_SKIP_P1_READINESS=1)。" >&2
  exit 1
fi

echo "[bootstrap] done. Optional: python3 scripts/openclaw_doctor.py --repo-root \"$WS_ROOT\""
