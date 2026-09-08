#!/usr/bin/env python3
"""DF-FanYi 通知助手 — 只显示最新一条, 永驻直到手动关闭。

三重保障(按序尝试, 全部失败也不影响发新通知):
1. omarchy IPC dismiss(按旧 summary 子串关屏上所有旧 toast)——
   quickshell 的 DBus CloseNotification 实测不生效(2026-09-08),
   但 omarchy-shell notifications dismiss 可靠(按 popupModel 匹配)。
2. DBus CloseNotification(旧 ID)——标准通知守护(dunst/mako)走这条。
3. Notify(replaces_id=旧 ID)——freedesktop 规范的原地替换,
   quickshell 实测: 旧 toast 被移除、新 toast 顶上(返回新 ID)。

永驻关键: urgency=critical — omarchy durationFor() 只对 Critical 返回 0
(永不消失); normal/low 被截断到 8s/5s(max 30s), timeout 无关紧要。

用法: notify.py [APP] [SUMMARY] [BODY]
状态文件: 每行 "通知ID" + "旧summary"(供下一条 dismiss 匹配)。
"""
import os
import shutil
import subprocess
import sys

import dbus

STATE = os.environ.get("NOTIFY_STATE_FILE") or os.path.expanduser("~/.unattended/.notif_id")


def read_state():
    """读上一条的状态 → (old_id | 0, old_summary | "")。兼容旧格式(只有 ID)。"""
    try:
        lines = open(STATE).read().splitlines()
        old_id = int(lines[0].strip()) if lines and lines[0].strip().isdigit() else 0
        old_summary = lines[1].strip() if len(lines) > 1 else ""
        return old_id, old_summary
    except Exception:
        return 0, ""


def dismiss_old_omarchy(old_summary: str) -> None:
    """omarchy 专属: 按 summary 子串关掉屏上所有旧 toast(含堆积的多条)。"""
    if not old_summary:
        return
    shell = shutil.which("omarchy-shell")
    if not shell:
        return
    try:
        subprocess.run(
            [shell, "-q", "notifications", "dismiss", old_summary],
            timeout=5, check=False,
        )
    except Exception:
        pass


def main():
    app = sys.argv[1] if len(sys.argv) > 1 else "DF-FanYi"
    summary = sys.argv[2] if len(sys.argv) > 2 else ""
    body = sys.argv[3] if len(sys.argv) > 3 else ""
    old_id, old_summary = read_state()

    # 1) omarchy IPC: 关掉屏上所有旧 toast(实测 CloseNotification 不生效, 这是可靠路径)
    dismiss_old_omarchy(old_summary)

    bus = dbus.SessionBus()
    obj = bus.get_object("org.freedesktop.Notifications", "/org/freedesktop/Notifications")
    iface = dbus.Interface(obj, "org.freedesktop.Notifications")

    # 2) 标准关闭(非 omarchy 的标准守护走这条; omarchy 上无害地失败)
    try:
        if old_id:
            iface.CloseNotification(dbus.UInt32(old_id))
    except Exception:
        pass

    # 永驻关键: 必须 urgency=critical — omarchy 的 durationFor() 只对
    # NotificationUrgency.Critical 返回 0(永不消失); normal/low 被截断到
    # 8s/5s(max 30s), 再大的 expireTimeout 也没用。
    hints = dbus.Dictionary({
        "urgency": dbus.Byte(2),  # critical
    }, signature="sv")

    # 3) replaces_id=旧 ID: freedesktop 规范原地替换(quickshell 实测移除旧 toast)
    nid = iface.Notify(app, dbus.UInt32(old_id or 0), "", summary, body, [], hints, dbus.Int32(0))

    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    with open(STATE, "w") as f:
        f.write("%s\n%s\n" % (nid, summary))
    print("notif_id=%s" % nid)


if __name__ == "__main__":
    main()
