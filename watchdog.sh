#!/bin/bash
# watchdog.sh — cron 每10分钟: driver 死了重启,完工则触发自停
REPO="/home/wu/DF-FanYi"
LOGF="$REPO/.unattended/watchdog.log"
SESSION="dffanyi-driver"
log(){ echo "[$(date '+%F %T')] $*" >> "$LOGF"; }
mkdir -p "$REPO/.unattended"

# 完工标记后静默退出
[ -f "$REPO/.unattended/STOPPED" ] && exit 0

# GUARD:quota-pause — 额度暂停尊重(保险①配套):
#   未到恢复时间 → 不拉起 driver(静默等待窗口重置)
#   到点 → 清暂停标记, 正常拉起 driver 续跑
PAUSE_F="$REPO/.unattended/PAUSED_QUOTA"
if [ -f "$PAUSE_F" ]; then
  RESUME_AT=$(cat "$PAUSE_F" 2>/dev/null || echo 0)
  NOW=$(date +%s)
  if [ "$NOW" -lt "$RESUME_AT" ]; then
    log "额度暂停中, $(date -d @$RESUME_AT '+%F %T') 恢复 — 本轮不拉起"
    exit 0
  fi
  log "额度窗口已重置 → 清暂停标记, 拉起 driver 续跑"
  rm -f "$PAUSE_F"
  "$REPO/scripts/notify.sh" "DF-FanYi" \
    "▶️ DF-FanYi 额度窗口重置" "无人值守自动续跑" "normal" 2>/dev/null || true
fi

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
