#!/bin/bash
# scripts/notify.sh — 统一通知通道(2026-09-12 重写)
# 规则:
#   1. 只显示最新一条: 同 app+headline 组合用 replaces_id 自动替换
#   2. 永驻: --expire-time=0, 手动点击/右键关闭
#   3. 健康检查: daemon 离线 → fallback 到 journalctl + .unattended/notification-lost.log, 不静默
#
# 用法:
#   notify.sh "DF-FanYi" "headline" "body" [urgency:low|normal|critical]
# 兼容性:
#   - 优先 notify-send (DBUS 标准)
#   - 兜底 gdbus call (不走 notify-send, 直接 D-Bus)
#   - 最后 journalctl + lost.log (永不静默)
set -u

APP="$1"
HEADLINE="$2"
BODY="${3:-}"
URGENCY="${4:-normal}"
STATE_DIR="/home/wu/DF-FanYi/.unattended"
STATE_FILE="$STATE_DIR/.notif_state"
LOST_LOG="$STATE_DIR/notification-lost.log"
mkdir -p "$STATE_DIR"

# 1. 健康检查: 检测通知 daemon 是否在跑
DAEMON_OK=false
DAEMON_NAME="none"
if command -v gdbus >/dev/null 2>&1; then
  NAMES=$(gdbus call --session --dest org.freedesktop.DBus \
    --object-path /org/freedesktop/DBus \
    --method org.freedesktop.DBus.ListNames 2>/dev/null)
  if echo "$NAMES" | grep -q "'org.freedesktop.Notifications'"; then
    DAEMON_OK=true
    OWNER_PID=$(busctl --user status org.freedesktop.Notifications 2>/dev/null | grep -oP 'PID=\K[0-9]+' | head -1)
    if [ -n "$OWNER_PID" ] && [ -r "/proc/$OWNER_PID/comm" ]; then
      DAEMON_NAME=$(cat "/proc/$OWNER_PID/comm" 2>/dev/null)
    else
      DAEMON_NAME="unknown"
    fi
  fi
fi

# 2. 找上一次同 (app, headline) 的 notification id, 用于替换
KEY="${APP}::${HEADLINE}"
PREV_ID=0
[ -f "$STATE_FILE" ] && PREV_ID=$(grep -F "$KEY" "$STATE_FILE" 2>/dev/null | tail -1 | awk '{print $NF}' | tr -d ':')
PREV_ID=${PREV_ID:-0}

# 3. 发送通知 (优先 gdbus 直接调, 可返回 ID 实现替换)
NEW_ID=""
SENT=false
if [ "$DAEMON_OK" = true ] && command -v gdbus >/dev/null 2>&1; then
  REPLY=$(gdbus call --session --dest org.freedesktop.Notifications \
    --object-path /org/freedesktop/Notifications \
    --method org.freedesktop.Notifications.Notify \
    --string: "$APP" --int32: "$PREV_ID" \
    --string: "$HEADLINE" --string: "$BODY" \
    --array:=:[] --dict:=:{} 2>&1)
  case "$REPLY" in
    *uint32*) SENT=true; NEW_ID=$(echo "$REPLY" | grep -oP 'uint32 \K[0-9]+' | head -1) ;;
  esac
fi

# 4. gdbus 失败回退到 notify-send (无 ID 返回, 用本地计数补)
if [ "$SENT" = false ] && command -v notify-send >/dev/null 2>&1; then
  notify-send --app-name="$APP" --urgency="$URGENCY" --expire-time=0 \
    --replace-id="$PREV_ID" "$HEADLINE" "$BODY" 2>&1
  if [ $? -eq 0 ]; then
    SENT=true
    # 本地单调递增 ID 兜底, 确保下次还能替换
    if [ "$PREV_ID" = "0" ]; then NEW_ID=$(date +%s); else NEW_ID="$PREV_ID"; fi
  fi
fi

# 5. 记录新 id (替换式)
if [ -n "${NEW_ID:-}" ]; then
  grep -v -F "$KEY" "$STATE_FILE" 2>/dev/null > "$STATE_FILE.tmp"
  echo "$KEY :: $NEW_ID" >> "$STATE_FILE.tmp"
  mv "$STATE_FILE.tmp" "$STATE_FILE"
fi

# 6. 成功路径
if [ "$SENT" = true ]; then
  echo "[$(date '+%F %T')] NOTIFY ok ($DAEMON_NAME) | $HEADLINE" >> "$STATE_DIR/notify.log"
  exit 0
fi

# 7. 失败路径: 永不静默
TS=$(date '+%F %T')
echo "[$TS] NOTIFY FAILED | daemon=$DAEMON_NAME alive=$DAEMON_OK | $HEADLINE :: $BODY" >> "$LOST_LOG"
journalctl --user -t DF-FanYi -n 0 --no-pager >/dev/null 2>&1 && \
  echo "[$TS] notify-lost: $HEADLINE :: $BODY" | systemd-cat -t DF-FanYi 2>/dev/null || true
# 仍然写到 stderr, 让定时任务 log 留下痕迹
echo "NOTIFY FAILED at $TS: $HEADLINE :: $BODY" >&2
exit 1
