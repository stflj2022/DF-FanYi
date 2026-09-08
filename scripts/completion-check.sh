#!/bin/bash
# completion-check.sh — 唯一完工判据: docs/tickets 全部 done 且 pytest 全绿
cd "$(dirname "$0")/.." || exit 1
total=$(ls docs/tickets/ticket-*.md 2>/dev/null | wc -l)
[ "$total" -eq 0 ] && { echo "无工单"; exit 1; }
done=$(grep -l "^## 状态: done" docs/tickets/ticket-*.md 2>/dev/null | wc -l)
echo "工单完成: $done/$total"
[ "$done" -lt "$total" ] && exit 1
python3 -m pytest tests/ -q >/dev/null 2>&1 || { echo "测试未全绿"; exit 1; }
echo "全部通过"
exit 0
