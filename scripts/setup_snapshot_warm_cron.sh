#!/usr/bin/env bash
# Install client-side snapshot warm cron (Asia/Shanghai).
# Runs after finance-ingest-cloud worker ingest windows.
#
# Usage:
#   ./scripts/setup_snapshot_warm_cron.sh --dry-run
#   ./scripts/setup_snapshot_warm_cron.sh --install
#   ./scripts/setup_snapshot_warm_cron.sh --remove
set -euo pipefail

WORKSPACE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WARM_SCRIPT="${WORKSPACE_ROOT}/scripts/warm_snapshot_cache.sh"
LOG_PATH="${FINANCE_SNAPSHOT_WARM_LOG:-${WORKSPACE_ROOT}/cache/snapshot/warm.log}"
CRON_TZ="${FINANCE_CRON_TZ:-Asia/Shanghai}"
CRON_TAG="# stream-gen snapshot warm"
CRON_BLOCK_BEGIN="# BEGIN stream-gen snapshot warm"
CRON_BLOCK_END="# END stream-gen snapshot warm"

CRON_SUFFIX=">> ${LOG_PATH} 2>&1 ${CRON_TAG}"
CRON_CMD="bash ${WARM_SCRIPT}"

# 08:15 早盘（云端 08:00 news 后）；09:45/14:05/20:05 晚于云端全量 5–10 分钟
CRON_LINES=(
  "15 8  * * * ${CRON_CMD} ${CRON_SUFFIX}"
  "45 9  * * * ${CRON_CMD} ${CRON_SUFFIX}"
  "5 14 * * * ${CRON_CMD} ${CRON_SUFFIX}"
  "5 20 * * * ${CRON_CMD} ${CRON_SUFFIX}"
)

strip_block() {
  local raw="$1"
  echo "${raw}" | awk -v begin="${CRON_BLOCK_BEGIN}" -v end="${CRON_BLOCK_END}" '
    $0 == begin { skip = 1; next }
    $0 == end   { skip = 0; next }
    !skip { print }
  ' | grep -vF "${CRON_TAG}" || true
}

MODE="dry-run"
for arg in "$@"; do
  case "${arg}" in
    --install) MODE="install" ;;
    --remove) MODE="remove" ;;
    --dry-run) MODE="dry-run" ;;
  esac
done

case "${MODE}" in
  dry-run)
    echo "=== stream-gen snapshot warm cron (--dry-run) ==="
    echo "  Script: ${WARM_SCRIPT}"
    echo "  Log:    ${LOG_PATH}"
    echo "  TZ:     ${CRON_TZ} (08:15 · 09:45 · 14:05 · 20:05)"
    echo ""
    echo "  ${CRON_BLOCK_BEGIN}"
    echo "  TZ=${CRON_TZ}"
    for line in "${CRON_LINES[@]}"; do echo "  ${line}"; done
    echo "  ${CRON_BLOCK_END}"
    ;;
  install)
    if [[ ! -x "${WARM_SCRIPT}" ]]; then
      chmod +x "${WARM_SCRIPT}" 2>/dev/null || true
    fi
    existing="$(crontab -l 2>/dev/null || true)"
    cleaned="$(strip_block "${existing}")"
    {
      echo "${cleaned}"
      echo "${CRON_BLOCK_BEGIN}"
      echo "TZ=${CRON_TZ}"
      for line in "${CRON_LINES[@]}"; do echo "${line}"; done
      echo "${CRON_BLOCK_END}"
    } | crontab -
    echo "Cron installed (08:15 · 09:45 · 14:05 · 20:05, TZ=${CRON_TZ})"
    ;;
  remove)
    existing="$(crontab -l 2>/dev/null || true)"
    strip_block "${existing}" | crontab -
    echo "Snapshot warm cron block removed."
    ;;
esac
