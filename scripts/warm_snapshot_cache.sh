#!/usr/bin/env bash
# 客户端快照预热：云端 pre-Router → 本机 db_snapshot（Router + Rewriter）→ cache/snapshot/snapshot.json
#
# 与 finance-ingest-cloud worker cron 对齐（CST）：
#   云端 08:00 news · 09:40/14:00/20:00 全量入库
#   本脚本建议 08:15 / 09:45 / 14:05 / 20:05（见 setup_snapshot_warm_cron.sh）
#
# Usage:
#   ./scripts/warm_snapshot_cache.sh
#   ./scripts/warm_snapshot_cache.sh --dry-run
set -euo pipefail

WORKSPACE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QMF="${WORKSPACE_ROOT}/skills/streamy-content-gen/scripts/query_market_facts.py"
CACHE="${WORKSPACE_ROOT}/cache/snapshot/snapshot.json"
LOG_DIR="${WORKSPACE_ROOT}/cache/snapshot"
LOG_PATH="${FINANCE_SNAPSHOT_WARM_LOG:-${LOG_DIR}/warm.log}"
TIMEOUT_SEC="${FINANCE_SNAPSHOT_WARM_TIMEOUT_SEC:-300}"
DRY_RUN=0

for arg in "$@"; do
  case "${arg}" in
    --dry-run) DRY_RUN=1 ;;
  esac
done

if [[ ! -f "${QMF}" ]]; then
  echo "warm_snapshot_cache: missing ${QMF}" >&2
  exit 1
fi

mkdir -p "${LOG_DIR}"

run_warm() {
  local ts
  ts="$(date -Iseconds)"
  echo "[${ts}] warm_snapshot_cache start (timeout=${TIMEOUT_SEC}s)" >> "${LOG_PATH}"
  if timeout "${TIMEOUT_SEC}" python3 "${QMF}" --sources market,news,social --full >> "${LOG_PATH}" 2>&1; then
    echo "[$(date -Iseconds)] warm_snapshot_cache ok cache=${CACHE}" >> "${LOG_PATH}"
    return 0
  fi
  echo "[$(date -Iseconds)] warm_snapshot_cache FAILED (see log tail)" >> "${LOG_PATH}"
  return 1
}

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "=== warm_snapshot_cache (--dry-run) ==="
  echo "  Workspace: ${WORKSPACE_ROOT}"
  echo "  Command:   timeout ${TIMEOUT_SEC} python3 ${QMF} --sources market,news,social --full"
  echo "  Cache:     ${CACHE}"
  echo "  Log:       ${LOG_PATH}"
  exit 0
fi

if run_warm; then
  if [[ -f "${CACHE}" ]]; then
    python3 - "${CACHE}" <<'PY'
import json, sys
p = sys.argv[1]
d = json.loads(open(p, encoding="utf-8").read())
meta = d.get("meta") or {}
print("ok:", d.get("ok", True))
print("fetched_at:", meta.get("fetched_at"))
print("router:", meta.get("llm_router_status"))
PY
    exit 0
  fi
  echo "warm_snapshot_cache: command succeeded but cache missing: ${CACHE}" >&2
  exit 2
fi
tail -n 30 "${LOG_PATH}" >&2 || true
exit 1
