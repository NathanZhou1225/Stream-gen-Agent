#!/usr/bin/env bash
# setup_cron.sh — 为 finance-source-ingest 安装/展示定时采集 cron 任务
#
# 用法：
#   ./setup_cron.sh --dry-run       # 只打印，不写入 crontab
#   ./setup_cron.sh --install       # 写入当前用户 crontab（会保留已有条目）
#   ./setup_cron.sh --remove        # 移除本脚本写入的 cron 条目
#
# 定时计划（服务器本地时间）：
#   每天 09:00、12:00、17:00 执行 ingest run
#
# 环境变量（可在调用前 export 覆盖）：
#   FINANCE_CRON_LOG_PATH   — 日志路径，默认 /tmp/finance_ingest_cron.log
#   FINANCE_DB_PATH         — DB 路径，默认 <workspace>/user_data/finance_sources.db

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INGEST_SCRIPT="${SCRIPT_DIR}/scripts/ingest.py"
LOG_PATH="${FINANCE_CRON_LOG_PATH:-/tmp/finance_ingest_cron.log}"

# 优先使用 finance-source-ingest .venv 内的 python
VENV_PYTHON="${SCRIPT_DIR}/.venv/bin/python"
if [[ -f "${VENV_PYTHON}" ]]; then
    PYTHON="${VENV_PYTHON}"
else
    PYTHON="$(command -v python3 || command -v python)"
fi

CRON_CMD="${PYTHON} ${INGEST_SCRIPT} run --sources market,news,social --max-items 30 >> ${LOG_PATH} 2>&1"
CRON_TAG="# finance-source-ingest auto-ingest"

CRON_LINES=(
    "0 9  * * 1-5 ${CRON_CMD}  ${CRON_TAG}"
    "0 12 * * 1-5 ${CRON_CMD}  ${CRON_TAG}"
    "0 17 * * 1-5 ${CRON_CMD}  ${CRON_TAG}"
)

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
        echo "Python:   ${PYTHON}"
        echo "Script:   ${INGEST_SCRIPT}"
        echo "Log:      ${LOG_PATH}"
        echo ""
        echo "将添加以下 crontab 条目（周一至周五 09:00 / 12:00 / 17:00）："
        echo ""
        for line in "${CRON_LINES[@]}"; do
            echo "  ${line}"
        done
        echo ""
        echo "运行 ./setup_cron.sh --install 安装，--remove 移除。"
        ;;

    install)
        existing_cron="$(crontab -l 2>/dev/null || true)"
        # 移除旧的同标签条目
        clean_cron="$(echo "${existing_cron}" | grep -v "${CRON_TAG}" || true)"
        new_cron="${clean_cron}"
        for line in "${CRON_LINES[@]}"; do
            new_cron="${new_cron}"$'\n'"${line}"
        done
        echo "${new_cron}" | crontab -
        echo "✅ Cron 任务已安装（周一至周五 09:00 / 12:00 / 17:00）"
        echo "   日志路径：${LOG_PATH}"
        echo "   查看：crontab -l | grep finance"
        ;;

    remove)
        existing_cron="$(crontab -l 2>/dev/null || true)"
        clean_cron="$(echo "${existing_cron}" | grep -v "${CRON_TAG}" || true)"
        echo "${clean_cron}" | crontab -
        echo "✅ Finance Newsbox cron 任务已移除。"
        ;;
esac
