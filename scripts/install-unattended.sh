#!/bin/bash
# install-unattended.sh — DF-FanYi 无人值守系统一键安装/重启
# 基于 unattended-dev-system skill v1.2.0 模板渲染
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SESSION="dffanyi-driver"
GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
log_info(){ echo -e "${GREEN}[INFO]${NC} $*"; }
log_error(){ echo -e "${RED}[ERROR]${NC} $*"; }

# 0. 清除旧完工标记(重新启用)
rm -f "$REPO/.unattended/STOPPED"
mkdir -p "$REPO/.unattended/tasks/"{todo,doing,done} "$REPO/.unattended/checkpoints"

# 1. 环境校验
for c in git tmux python3 pi systemctl; do
  command -v "$c" >/dev/null || { log_error "缺命令: $c"; exit 1; }
done
ssh -T git@github.com 2>&1 | grep -q "successfully authenticated" || log_error "GitHub SSH 未就绪,auto-push 会失败"

# 2. 渲染 driver.sh(来自 unattended-dev-system 模板,若项目内无备份则从 skill 取)
TPL="/home/wu/.pi/agent/skills/unattended-dev-system/templates/driver.sh.template"
[ -f "$REPO/scripts/driver.sh.template" ] && TPL="$REPO/scripts/driver.sh.template"
AI_COMMAND="pi --print --no-session --mode text -p '无人值守驱动. 读取 docs/tickets 目录,找到编号最小的状态为 pending 且其 Blocked by 引用的工单全部 done 的工单. 严格按该工单实现,遵循 docs/specs 与工程书强制禁止事项,测试驱动开发. 完成后把该工单状态行改为 done,复制该文件到 .unattended/tasks/done 目录,运行 python3 -m pytest tests/ -q 确认全绿,然后 git add -A 并 git commit. 若无可做工单,检查全部工单状态,输出进度总结并改进代码质量'"
sed -e "s|{{PROJECT_NAME}}|DF-FanYi|g" \
    -e "s|{{LANGUAGE}}|Python|g" \
    -e "s|{{FRAMEWORK}}|pytest|g" \
    -e "s|{{PROJECT_ROOT}}|$REPO|g" \
    -e 's|{{PROVIDERS}}|"pi-primary"|g' \
    -e "s|{{TEST_COMMAND}}|python3 -m pytest tests/ -q|g" \
    -e "s|{{GIT_AUTO_PUSH}}|true|g" \
    -e "s|{{COMMIT_PREFIX}}|chore(driver)|g" \
    -e "s|{{DRIVER_TIMEOUT}}|7200|g" \
    -e "s|{{ZERO_OUTPUT_FUSE}}|900|g" \
    -e "s|{{AI_COMMAND}}|$AI_COMMAND|g" \
    "$TPL" > "$REPO/driver.sh"
chmod +x "$REPO/driver.sh"
bash -n "$REPO/driver.sh" || { log_error "driver.sh 语法错误"; exit 1; }

# 3.5 完工判定修正: 模板的 all_done() 基于 tasks/ 目录(工单在 docs/tickets 时
#     会误判“无待办=全完成”)。补丁为基于工单状态行判定。
python3 - "$REPO/driver.sh" << 'PYEOF'
import sys
p = sys.argv[1]
s = open(p).read()
old = '''all_done() {
    local done total
    done=$(ls "$TASKS_DIR/done/" 2>/dev/null | wc -l)
    total=$(( $(ls "$TASKS_DIR"/todo/ 2>/dev/null | wc -l) \\
            + $(ls "$TASKS_DIR"/doing/ 2>/dev/null | wc -l) \\
            + done ))
    [ "$total" -gt 0 ] && [ "$done" -ge "$total" ]
}'''
new = '''all_done() {
    local done total
    total=$(ls "$REPO/docs/tickets"/ticket-*.md 2>/dev/null | wc -l)
    [ "$total" -eq 0 ] && return 1
    done=$(grep -l "^## 状态: done" "$REPO/docs/tickets"/ticket-*.md 2>/dev/null | wc -l)
    [ "$done" -ge "$total" ]
}'''
assert old in s, "all_done 原文未匹配,停止"
open(p, 'w').write(s.replace(old, new))
print("✓ all_done 已补丁为基于工单状态行")
PYEOF
bash -n "$REPO/driver.sh" || { log_error "补丁后 driver.sh 语法错误"; exit 1; }

# 3. config.json(driver 运行时读取,覆盖模板默认值)
cat > "$REPO/.unattended/config.json" << EOF
{
  "driver": { "providers": ["pi-primary"], "check_interval": 300,
              "timeout": 7200, "zero_output_fuse": 900, "max_retries": 3, "dry_run": false },
  "watchdog": { "check_interval_minutes": 10, "stuck_threshold_minutes": 45 },
  "testing": { "command": "python3 -m pytest tests/ -q", "auto_commit": true },
  "git": { "auto_push": true, "commit_message_format": "chore(driver)" },
  "project": { "name": "DF-FanYi", "language": "Python", "root": "$REPO" }
}
EOF

# 4. systemd 用户 timer watchdog(幂等安装,替代 cron)
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/dffanyi-watchdog.service << EOF
[Unit]
Description=DF-FanYi watchdog (driver health check)

[Service]
Type=oneshot
ExecStart=$REPO/watchdog.sh
EOF
cat > ~/.config/systemd/user/dffanyi-watchdog.timer << 'EOF'
[Unit]
Description=DF-FanYi watchdog timer (every 10 min)

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now dffanyi-watchdog.timer
log_info "systemd timer watchdog 已安装(每10分钟)"

# 4.5 进度通知 timer(每30分钟桌面通知,幂等)
cat > ~/.config/systemd/user/dffanyi-progress-report.service << EOF
[Unit]
Description=DF-FanYi progress report notification

[Service]
Type=oneshot
ExecStart=$REPO/scripts/progress-report.sh
EOF
cat > ~/.config/systemd/user/dffanyi-progress-report.timer << 'EOF'
[Unit]
Description=DF-FanYi progress report timer (every 30 min)

[Timer]
OnBootSec=30min
OnUnitActiveSec=30min
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now dffanyi-progress-report.timer
log_info "进度通知 timer 已安装(每30分钟)"

# 5. 启动 driver(tmux,幂等)
tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION" "cd $REPO && bash driver.sh >> $REPO/.unattended/driver.stdout 2>&1"
sleep 3
if tmux has-session -t "$SESSION" 2>/dev/null; then
  log_info "✅ driver 已启动 (tmux: $SESSION)"
else
  log_error "driver 启动失败,查看 $REPO/.unattended/driver.stdout"; exit 1
fi
log_info "观察: tail -f $REPO/.unattended/driver.log"
log_info "完工判据: bash $REPO/scripts/completion-check.sh && echo 已完工"
log_info "看门狗: systemctl --user status dffanyi-watchdog.timer"
log_info "进度通知: systemctl --user status dffanyi-progress-report.timer"
