#!/bin/bash
# shutdown.sh — 完工/手动自停(幂等): 写标记 + 杀 tmux + 撤 cron + 一次终报
REPO="/home/wu/DF-FanYi"
LOGF="$REPO/.unattended"
REASON="${1:-手动停止}"
mkdir -p "$LOGF"
touch "$LOGF/STOPPED"
tmux kill-session -t dffanyi-driver 2>/dev/null
systemctl --user disable --now dffanyi-watchdog.timer 2>/dev/null
systemctl --user disable --now dffanyi-progress-report.timer 2>/dev/null
echo "[$(date '+%F %T')] shutdown: $REASON" >> "$LOGF/watchdog.log"
# 一次性终报
DONE=$(grep -l "^## 状态: done" "$REPO"/docs/tickets/ticket-*.md 2>/dev/null | wc -l)
TOTAL=$(ls "$REPO"/docs/tickets/ticket-*.md 2>/dev/null | wc -l)
"$REPO/scripts/notify.sh" "DF-FanYi" \
  "🏁 DF-FanYi 无人值守已停止" "工单: $DONE/$TOTAL 完成 · 原因: $REASON" 2>/dev/null
echo "已停止: $REASON (工单 $DONE/$TOTAL)"
