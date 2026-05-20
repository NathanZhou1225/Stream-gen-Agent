#!/usr/bin/env bash
# 业务机 workspace 更新：对拍远端 → pull → bootstrap 校验
set -euo pipefail

WS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$WS_ROOT"

CLEAR_SNAPSHOT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --clear-snapshot-cache)
      CLEAR_SNAPSHOT=1
      shift
      ;;
    *)
      echo "[upgrade] 未知参数: $1（支持 --clear-snapshot-cache）" >&2
      exit 2
      ;;
  esac
done

echo "[upgrade] workspace_root=$WS_ROOT"

if [[ "${CLEAR_SNAPSHOT}" == "1" ]]; then
  rm -f cache/snapshot/snapshot.json cache/snapshot/markdown_summary.md
  echo "[upgrade] cleared cache/snapshot/*"
fi

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
echo "  ./scripts/present_today_snapshot.sh"
echo "  python3 skills/streamy-content-gen/scripts/query_market_facts.py --sources market,news,social --summary-only"
echo "[upgrade] zip 更新后建议: ./scripts/upgrade.sh --clear-snapshot-cache && present_today_snapshot.sh --refresh"
