#!/bin/bash
# progress-report.sh — 每30分钟一次进度通知(自动工程工作流机制)
# 通知策略: 手动点击才消失 + 只显示最新一条
#   通道1 dunst/swaync/mako: notify-send -t 0(永驻) + 固定 app 名(daemon 按 app 自动替换旧条)
#   通道2 无守护进程时: Hyprland hyprctl notify(timeout 0 常驻; 无点击关闭能力,属降级)
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

# 通道选择: 1) 标准通知守护(quickshell/dunst/swaync/mako) → dbus 关旧发新
#           2) 无守护时降级 Hyprland 内置 notify(无法点击关闭)
if python3 -c 'import dbus' >/dev/null 2>&1; then
  NOTIFY_STATE_FILE="$REPO/.unattended/.notif_id" python3 "$REPO/scripts/notify.py" "DF-FanYi" "🏗️ DF-FanYi 无人值守进度 $DONE/$TOTAL" "$MSG" 2>>"$LOGF" || \
    notify-send --app-name="DF-FanYi" -t 0 "🏗️ DF-FanYi $DONE/$TOTAL" "$MSG"
elif command -v hyprctl >/dev/null 2>&1; then
  MSG_FLAT=$(echo "$MSG" | tr '\n' ' ')
  hyprctl notify -1 0 0xff44aa88 "🏗️ DF-FanYi $DONE/$TOTAL $MSG_FLAT" >/dev/null 2>&1
else
  echo "[$(date '+%F %T')] 无通知通道,MSG=$MSG" >> "$LOGF"
fi
