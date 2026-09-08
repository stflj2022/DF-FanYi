#!/usr/bin/env python3
"""DF-FanYi 通知助手 — 只显示最新一条, 永驻直到手动关闭。
发新通知前关闭上一条记录的通知 ID(CloseNotification), 然后 Notify(timeout=0)。
依赖: python-dbus(quickshell/dunst 等任何标准 org.freedesktop.Notifications 实现均可)。
用法: notify.py [APP] [SUMMARY] [BODY]
"""
import sys, os, dbus

STATE = os.environ.get("NOTIFY_STATE_FILE") or os.path.expanduser("~/.unattended/.notif_id")

def main():
    app = sys.argv[1] if len(sys.argv) > 1 else "DF-FanYi"
    summary = sys.argv[2] if len(sys.argv) > 2 else ""
    body = sys.argv[3] if len(sys.argv) > 3 else ""
    bus = dbus.SessionBus()
    obj = bus.get_object("org.freedesktop.Notifications", "/org/freedesktop/Notifications")
    iface = dbus.Interface(obj, "org.freedesktop.Notifications")
    # 关闭上一条(只留最新)
    try:
        old = open(STATE).read().strip()
        if old.isdigit():
            iface.CloseNotification(dbus.UInt32(int(old)))
    except Exception:
        pass
    # 永驻关键: 必须 urgency=critical — omarchy 的 durationFor() 只对
    # NotificationUrgency.Critical 返回 0(永不消失); normal/low 被截断到
    # 8s/5s(max 30s), 再大的 expireTimeout 也没用。
    # spec: timeout=0 由服务器决定; 配合 critical 才真正永驻。
    hints = dbus.Dictionary({
        "urgency": dbus.Byte(2),  # critical
    }, signature="sv")
    nid = iface.Notify(app, dbus.UInt32(0), "", summary, body, [], hints, dbus.Int32(0))
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    open(STATE, "w").write(str(nid))
    print("notif_id=%s" % nid)

if __name__ == "__main__":
    main()
