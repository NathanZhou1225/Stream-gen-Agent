#!/bin/bash
# Stream-Gen 会话清理脚本
# 定期清理旧的会话备份和重置文件

SESSION_DIR="/root/.openclaw/agents/stream-gen/sessions"

echo "=== Stream-Gen Session Cleanup ==="
echo "Before cleanup:"
du -sh "$SESSION_DIR"
find "$SESSION_DIR" -name "*.jsonl*" | wc -l

# 1. 删除超过7天的.reset备份文件
echo ""
echo "1. Cleaning .reset files older than 7 days..."
find "$SESSION_DIR" -name "*.reset*" -type f -mtime +7 -delete

# 2. 删除超过14天的非活跃会话文件（保留最近5个活跃文件）
echo ""
echo "2. Cleaning old session files (keep most recent 5)..."
cd "$SESSION_DIR"
ls -t *.jsonl 2>/dev/null | tail -n +6 | xargs -r rm -v

echo ""
echo "After cleanup:"
du -sh "$SESSION_DIR"
find "$SESSION_DIR" -name "*.jsonl*" | wc -l
echo "=== Done ==="
