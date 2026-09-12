# scripts/notify.sh — 统一通知通道

## 解决的问题
**DF-FanYi 无人值守系统的所有通知（30分钟进度、工单暂停/恢复、完工终报、熔断停机）原本通过 `notify-send` 直接发送**——如果系统里没有任何通知 daemon（quickshell/mako/dunst/swaync/fnott），`notify-send` 会静默吞掉消息（退出码 0 但消息消失）。

**事故**：2026-09-12 SPEC-012 全部 16 工单完工，用户没收到任何通知，因为 quickshell 是 omarchy 自带的（不是普通 "需要手动装的 daemon"），但本机通知守护进程并不像传统 Linux 桌面那样显眼。

## 解决
1. **健康检查**：脚本启动时通过 DBUS 查 `org.freedesktop.Notifications` bus 的 owner
2. **替换式**：每个 (app, headline) 组合记录上一次的通知 ID，DBUS Notify 调用时用 `replace_id`，只显示最新一条
3. **永驻**：`--expire-time=0`，用户手动点击/右键关闭（按用户要求）
4. **永不静默失败**：daemon 离线 → 写 `.unattended/notification-lost.log` + `journalctl -t DF-FanYi` + stderr

## 用法
```bash
scripts/notify.sh "DF-FanYi" "headline 文本" "body 多行文本" "urgency:low|normal|critical"
```

## 检测到的守护进程
- **quickshell** (Omarchy/Hyprland 默认)
- **mako** (Wayland 标准)
- **dunst / swaync / fnott** (其他 WM)
- 任意 D-Bus 拥有 `org.freedesktop.Notifications` 名的进程

## 状态文件
- `.unattended/.notif_state` — (app, headline) → notif_id 映射，实现替换
- `.unattended/notify.log` — 每次发送结果（ok/failed）
- `.unattended/notification-lost.log` — daemon 离线时的死信（永不丢失）
