#!/usr/bin/env bash
# 在「当前这份」 finance-source-ingest 根目录下创建/修复 .venv（路径无关，可反复执行）。
# 用法：在 skill 根目录执行  ./scripts/bootstrap_venv.sh
#   或：bash /path/to/finance-source-ingest/scripts/bootstrap_venv.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"
VENV="$ROOT/.venv"

if [[ "${BOOTSTRAP_FORCE:-0}" == "1" ]] && [[ -d "$VENV" ]]; then
  echo "[bootstrap_venv] BOOTSTRAP_FORCE=1 -> 删除旧 .venv"
  rm -rf "$VENV"
fi

if [[ ! -d "$VENV" ]]; then
  echo "[bootstrap_venv] 创建 venv: $VENV"
  "$PY" -m venv "$VENV"
else
  echo "[bootstrap_venv] 已存在 .venv，仅安装/升级依赖（设 BOOTSTRAP_FORCE=1 可删后重建）"
fi

PIP=( "$VENV/bin/python" -m pip )
"${PIP[@]}" install -U pip setuptools wheel
"${PIP[@]}" install -r "$ROOT/requirements.txt"
echo "[bootstrap_venv] 完成。请始终用以下方式调用 ingest（避免用到系统 python）："
echo "  $VENV/bin/python scripts/ingest.py run --sources market,news --max-items 3"
echo "或:  $VENV/bin/python -m pip show akshare"
