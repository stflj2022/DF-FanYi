#!/bin/bash
# progress-report.sh — 每30分钟一次进度通知(自动工程工作流机制)
# 通知策略 (2026-09-12 修订):
#   - 只显示最新一条: 统一走 scripts/notify.sh, 同 headline 自动替换
#   - 永驻: --expire-time=0, 用户手动点击/右键关闭
#   - 永不死信: notify.sh 检测 daemon 离线→写 notification-lost.log
#   - 完工后静默
REPO="/home/wu/DF-FanYi"
LOGF="$REPO/.unattended/progress-report.log"
mkdir -p "$REPO/.unattended"

# 完工后静默
[ -f "$REPO/.unattended/STOPPED" ] && exit 0

DONE=$(grep -l -e "^## 状态: done" -e "^> .*状态：done" "$REPO"/docs/tickets/ticket-*.md 2>/dev/null | wc -l)
TOTAL=$(ls "$REPO"/docs/tickets/ticket-*.md 2>/dev/null | wc -l)
RUNNING="否"
tmux has-session -t dffanyi-driver 2>/dev/null && RUNNING="是"
LAST=$(cd "$REPO" && git log -1 --format="%h %ad %s" --date=format:"%H:%M" 2>/dev/null)
NEXT_TICKET=$(grep -L -e "^## 状态: done" -e "^> .*状态：done" "$REPO"/docs/tickets/ticket-*.md 2>/dev/null | head -1 | xargs -r basename 2>/dev/null)

MSG="工单: $DONE/$TOTAL 完成 · driver 运行中:$RUNNING
最近提交: $LAST"
[ -n "$NEXT_TICKET" ] && MSG="$MSG
下一张: $NEXT_TICKET"

echo "[$(date '+%F %T')] $DONE/$TOTAL | driver=$RUNNING | $LAST" >> "$LOGF"

# 统一通知入口 (2026-09-12: 替换旧 python3 notify.py + 直接 notify-send)
# 只显示最新一条 + 永驻 + 自动替换(同 headline)
HEADLINE="🏗️ DF-FanYi 进度 $DONE/$TOTAL"
"$REPO/scripts/notify.sh" "DF-FanYi" "$HEADLINE" "$MSG" "normal" 2>>"$LOGF" || \
  echo "[$(date '+%F %T')] notify.sh 失败(见 notification-lost.log), MSG=$MSG" >> "$LOGF"