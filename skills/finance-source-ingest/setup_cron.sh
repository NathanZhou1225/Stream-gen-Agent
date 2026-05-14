#!/usr/bin/env bash
# setup_cron.sh — 为 finance-source-ingest 安装/展示定时采集 cron 任务
#
# 用法：
#   ./setup_cron.sh --dry-run       # 只打印，不写入 crontab
#   ./setup_cron.sh --install       # 写入当前用户 crontab（会保留已有条目）
#   ./setup_cron.sh --remove        # 移除本脚本写入的 cron 条目
#
# 定时计划（北京时间 CST，与 ingest 输出 meta.timezone 一致）：
#   工作日（周一～五）09:00、14:00、20:00 执行 ingest run
#   通过 crontab 内 TZ=… 指定解释时区；避免主机为 UTC 时误把 9/14/20 当成 UTC 点。
#
# 环境变量（可在调用前 export 覆盖）：
#   FINANCE_CRON_TZ         — 默认 Asia/Shanghai（北京时间）
#   FINANCE_CRON_LOG_PATH   — 日志路径，默认 /tmp/finance_ingest_cron.log
#   FINANCE_DB_PATH         — DB 路径，默认 <workspace>/user_data/finance_sources.db

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INGEST_SCRIPT="${SCRIPT_DIR}/scripts/ingest.py"
DB_SNAPSHOT_SCRIPT="${WORKSPACE_ROOT}/skills/finance-draft-manager/scripts/db_snapshot.py"
CACHE_DIR="${WORKSPACE_ROOT}/cache/snapshot"
LOG_PATH="${FINANCE_CRON_LOG_PATH:-/tmp/finance_ingest_cron.log}"

# 优先使用 finance-source-ingest .venv 内的 python
VENV_PYTHON="${SCRIPT_DIR}/.venv/bin/python"
if [[ -f "${VENV_PYTHON}" ]]; then
    PYTHON="${VENV_PYTHON}"
else
    PYTHON="$(command -v python3 || command -v python)"
fi

# 入库命令 + 快照缓存命令
CRON_CMD="${PYTHON} ${INGEST_SCRIPT} run --sources market,news,social --max-items 30 >> ${LOG_PATH} 2>&1 && mkdir -p ${CACHE_DIR} && ${PYTHON} ${DB_SNAPSHOT_SCRIPT} --out-dir ${CACHE_DIR} --summary-only >> ${LOG_PATH} 2>&1"
CRON_TAG="# finance-source-ingest auto-ingest"
CRON_TZ="${FINANCE_CRON_TZ:-Asia/Shanghai}"
CRON_BLOCK_BEGIN="# BEGIN finance-source-ingest Newsbox cron"
CRON_BLOCK_END="# END finance-source-ingest Newsbox cron"

CRON_LINES=(
    "0 9  * * 1-5 ${CRON_CMD}  ${CRON_TAG}"
    "0 14 * * 1-5 ${CRON_CMD}  ${CRON_TAG}"
    "0 20 * * 1-5 ${CRON_CMD}  ${CRON_TAG}"
)

# 去掉本脚本曾写入的整块（含 TZ），并去掉仅带 CRON_TAG 的旧行（无块标记时的遗留）
strip_finance_cron_block() {
    local raw="$1"
    echo "${raw}" | awk -v begin="${CRON_BLOCK_BEGIN}" -v end="${CRON_BLOCK_END}" '
        $0 == begin { skip = 1; next }
        $0 == end   { skip = 0; next }
        !skip { print }
    ' | grep -vF "${CRON_TAG}" || true
}

# ── 模式解析 ─────────────────────────────────────────────────────────────────
MODE="dry-run"
for arg in "$@"; do
    case "${arg}" in
        --install) MODE="install" ;;
        --remove)  MODE="remove"  ;;
        --dry-run) MODE="dry-run" ;;
    esac
done

case "${MODE}" in
    dry-run)
        echo "=== Finance Newsbox — Cron 定时任务（--dry-run，未写入）==="
        echo "  Python:   ${PYTHON}"
        echo "  Ingest:   ${INGEST_SCRIPT}"
        echo "  Snapshot: ${DB_SNAPSHOT_SCRIPT}"
        echo "  Cache:    ${CACHE_DIR}/snapshot.json"
        echo "  Log:      ${LOG_PATH}"
        echo ""
        echo "将添加以下 crontab 块（周一至周五 09:00 / 14:00 / 20:00，按 TZ=${CRON_TZ} 即北京时间）："
        echo ""
        echo "  ${CRON_BLOCK_BEGIN}"
        echo "  TZ=${CRON_TZ}"
        for line in "${CRON_LINES[@]}"; do
            echo "  ${line}"
        done
        echo "  ${CRON_BLOCK_END}"
        echo ""
        echo "运行 ./setup_cron.sh --install 安装，--remove 移除。"
        ;;

    install)
        existing_cron="$(crontab -l 2>/dev/null || true)"
        clean_cron="$(strip_finance_cron_block "${existing_cron}")"
        block=$(
            printf '%s\n' "${CRON_BLOCK_BEGIN}"
            printf 'TZ=%s\n' "${CRON_TZ}"
            printf '%s\n' "${CRON_LINES[@]}"
            printf '%s\n' "${CRON_BLOCK_END}"
        )
        new_cron="${clean_cron}"$'\n'"${block}"
        echo "${new_cron}" | crontab -
        echo "✅ Cron 任务已安装（周一至周五 09:00 / 14:00 / 20:00，时区 TZ=${CRON_TZ}）"
        echo "   日志路径：${LOG_PATH}"
        echo "   查看：crontab -l | grep finance"
        ;;

    remove)
        existing_cron="$(crontab -l 2>/dev/null || true)"
        clean_cron="$(strip_finance_cron_block "${existing_cron}")"
        echo "${clean_cron}" | crontab -
        echo "✅ Finance Newsbox cron 任务已移除。"
        ;;
esac
