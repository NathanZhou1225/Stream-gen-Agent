#!/usr/bin/env bash
# 业务机 workspace 更新：对拍远端 → pull → bootstrap 校验
set -euo pipefail

WS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$WS_ROOT"

echo "[upgrade] workspace_root=$WS_ROOT"

if [[ -d .git ]]; then
  echo "[upgrade] git fetch..."
  git fetch origin 2>/dev/null || git fetch 2>/dev/null || true
  echo "[upgrade] git status:"
  git status -sb || true
  if git rev-parse --abbrev-ref HEAD@{upstream} >/dev/null 2>&1; then
    behind="$(git rev-list --count HEAD..@{upstream} 2>/dev/null || echo 0)"
    echo "[upgrade] commits behind upstream: ${behind}"
  fi
  echo "[upgrade] git pull --ff-only (失败时请手动处理冲突)..."
  git pull --ff-only || git pull
else
  echo "[upgrade] 警告: 无 .git，无法 pull；请用 git clone 或 zip 整包替换（备份 .env）。" >&2
fi

if [[ -f .env.example ]] && [[ -f .env ]]; then
  echo "[upgrade] 提示: 对照 .env.example 检查是否有新增变量。"
fi

chmod +x download_and_install.sh 2>/dev/null || true
./download_and_install.sh

echo "[upgrade] 可选冒烟:"
echo "  python3 skills/streamy-content-gen/scripts/query_market_facts.py --sources market,news,social --summary-only"
