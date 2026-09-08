#!/bin/bash
# watchdog.sh — cron 每10分钟: driver 死了重启,完工则触发自停
REPO="/home/wu/DF-FanYi"
LOGF="$REPO/.unattended/watchdog.log"
SESSION="dffanyi-driver"
log(){ echo "[$(date '+%F %T')] $*" >> "$LOGF"; }
mkdir -p "$REPO/.unattended"

# 完工标记后静默退出
[ -f "$REPO/.unattended/STOPPED" ] && exit 0

# 完工判据成立 → 自停(driver 内部也会自查,此为双保险)
if bash "$REPO/scripts/completion-check.sh" >/dev/null 2>&1; then
  log "完工判据成立 → shutdown"
  bash "$REPO/shutdown.sh" "watchdog 检测到完工" >> "$LOGF" 2>&1
  exit 0
fi

# driver 活着吗
if tmux has-session -t "$SESSION" 2>/dev/null; then
  log "driver 正常"
  exit 0
fi

# 防抖: 5 分钟内重启过则跳过(等待 driver 自己起来/日志判读)
now=$(date +%s)
last=$(cat "$REPO/.unattended/.wd_last_restart" 2>/dev/null || echo 0)
if [ $((now - last)) -lt 300 ]; then
  log "300s 内已重启过,等待观察"
  exit 0
fi
echo "$now" > "$REPO/.unattended/.wd_last_restart"
log "driver 不在 → tmux 重启"
tmux new-session -d -s "$SESSION" "cd $REPO && bash driver.sh >> $REPO/.unattended/driver.stdout 2>&1"
