#!/usr/bin/env bash
# 快照 cache 监控 cron（可选）：每小时写 monitor.log + 失败时 stderr
#
# Usage:
#   ./scripts/setup_snapshot_monitor_cron.sh --dry-run
#   ./scripts/setup_snapshot_monitor_cron.sh --install
#   ./scripts/setup_snapshot_monitor_cron.sh --remove
set -euo pipefail

WORKSPACE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MONITOR_SCRIPT="${WORKSPACE_ROOT}/scripts/monitor_snapshot_cache.py"
LOG_PATH="${FINANCE_SNAPSHOT_MONITOR_LOG:-${WORKSPACE_ROOT}/cache/snapshot/monitor.log}"
CRON_TAG="# stream-gen snapshot monitor"
CRON_BLOCK_BEGIN="# BEGIN stream-gen snapshot monitor"
CRON_BLOCK_END="# END stream-gen snapshot monitor"

CRON_SUFFIX=">> ${LOG_PATH} 2>&1 ${CRON_TAG}"
# 每小时 :20 跑（避开 warm 的 :05/:15/:45）
CRON_LINE="20 * * * * python3 ${MONITOR_SCRIPT} --append-log ${CRON_SUFFIX}"

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
  esac
done

case "${MODE}" in
  dry-run)
    echo "=== snapshot monitor cron (--dry-run) ==="
    echo "  Monitor: ${MONITOR_SCRIPT}"
    echo "  Log:     ${LOG_PATH}"
    echo "  Line:    ${CRON_LINE}"
    ;;
  install)
    existing="$(crontab -l 2>/dev/null || true)"
    {
      strip_block "${existing}"
      echo "${CRON_BLOCK_BEGIN}"
      echo "${CRON_LINE}"
      echo "${CRON_BLOCK_END}"
    } | crontab -
    echo "Installed snapshot monitor cron → ${LOG_PATH}"
    ;;
  remove)
    existing="$(crontab -l 2>/dev/null || true)"
    strip_block "${existing}" | crontab -
    echo "Removed snapshot monitor cron block"
    ;;
esac
