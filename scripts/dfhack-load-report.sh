#!/usr/bin/env bash
# dfhack-load-report.sh — 真实负载探针(ticket-008 验收: "Lua 桥能在 DF 中加载,
# 引擎关闭时游戏正常显示原文")。
#
# 流程:
#   1. 确保 fanyi.lua 已安装进 hack/scripts/(未装则尝试 install)
#   2. 确保 Python 翻译引擎桥在监听(默认 127.0.0.1:17486, 未起则后台拉起)
#   3. 若游戏正在运行 → 通过 dfhack-run 加载报告:
#        dfhack-run fanyi status     (引擎在线/统计)
#        可选 --capture: dfhack-run fanyi start
#   4. 若游戏未运行 → 给出在游戏中手工验证的步骤
#   --die:     结束本探针拉起的引擎桥(不含 --capture 时默认退出前清理)
#   --no-engine: 跳过引擎启动(专门验证引擎关闭时游戏静默原文/零影响)
#
# 安全: 不 kill 游戏; --die 只结束引擎桥进程。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DF_ROOT="${DF_ROOT:-$HOME/Games/DwarfFortress}"
HOST=127.0.0.1
PORT="${FANYI_PORT:-17486}"
ENGINE_PID=""
DIEMODE=0
CAPTURE=0
NO_ENGINE=0
for a in "$@"; do
    case "$a" in
        --die) DIEMODE=1 ;;
        --capture) CAPTURE=1 ;;
        --no-engine) NO_ENGINE=1 ;;
        *) echo "未知参数: $a" >&2; exit 2 ;;
    esac
done

say() { printf '\n== %s ==\n' "$*"; }

say "DF-FanYi 真实负载探针"
echo "  DF 根目录 : $DF_ROOT"
echo "  引擎端口  : $HOST:$PORT"

# 1. 安装检查
if [[ ! -f "$DF_ROOT/hack/scripts/fanyi.lua" ]] ||
   ! diff -q "$ROOT/dfhack/scripts/fanyi.lua" "$DF_ROOT/hack/scripts/fanyi.lua" >/dev/null 2>&1; then
    bash "$ROOT/scripts/install-dfhack.sh" "$DF_ROOT" install
fi
echo "  fanyi.lua 已就位 ✓"

# 2. 引擎桥
ENGINE_LISTENING=0
if python3 - "$HOST" "$PORT" <<'EOF' >/dev/null 2>&1
import socket, sys
s = socket.socket()
s.settimeout(0.4)
s.connect((sys.argv[1], int(sys.argv[2])))
s.close()
EOF
then
    ENGINE_LISTENING=1
fi

if [[ "$NO_ENGINE" == "1" ]]; then
    if [[ "$ENGINE_LISTENING" == "1" ]]; then
        echo "  引擎桥已在监听(注意: --no-engine 模式期望它关闭)"
    else
        echo "  --no-engine: 不启动引擎桥(游戏侧将静默显示原文, §2.3)"
    fi
elif [[ "$ENGINE_LISTENING" == "1" ]]; then
    echo "  引擎桥已在监听 ✓"
else
    say 启动翻译引擎桥
    (cd "$ROOT" && nohup python3 -m df_fanyi bridge --transport tcp --port "$PORT" \
        > /tmp/df-fanyi-engine.log 2>&1 & echo $! > /tmp/df-fanyi-engine.pid)
    ENGINE_PID="$(cat /tmp/df-fanyi-engine.pid 2>/dev/null || true)"
    echo "  引擎桥已后台启动 pid=$ENGINE_PID (日志 /tmp/df-fanyi-engine.log)"
    # 等端口就绪(最多 15s)
    for _ in $(seq 1 30); do
        if python3 - "$HOST" "$PORT" <<'PYEOF' >/dev/null 2>&1
import socket, sys
s = socket.socket()
s.settimeout(0.5)
s.connect((sys.argv[1], int(sys.argv[2])))
s.close()
PYEOF
        then break; fi
        sleep 0.5
    done
fi

cleanup() {
    if [[ -n "$ENGINE_PID" ]]; then
        kill "$ENGINE_PID" 2>/dev/null || true
        echo "  引擎桥(pid=$ENGINE_PID)已清理 --die"
    fi
}
trap cleanup EXIT

# 3. 游戏侧
GAME_RUNNING=0
if command -v dfhack-run >/dev/null 2>&1 &&
   timeout 3 dfhack-run -q "generate documents" >/dev/null 2>&1; then
    GAME_RUNNING=1
fi
if [[ "$GAME_RUNNING" == "1" ]]; then
    say "游戏运行中 — dfhack-run 载入报告"
    if timeout 10 dfhack-run "fanyi status" >/tmp/fanyi-status.out 2>&1; then
        cat /tmp/fanyi-status.out
    else
        echo "  $DF_ROOT/hack/scripts/fanyi.lua 未被我方签名(load 失败)?" >&2
        cat /tmp/fanyi-status.out >&2
        exit 3
    fi
    if [[ "$CAPTURE" == "1" ]]; then
        dfhack-run "fanyi start"
        echo "  捕获已开启: 引擎离线/宕机时游戏继续显示原文(§2.3), 断线自动退避重连。"
    fi
else
    say "游戏未运行 — 手工验证步骤"
    cat <<'EOF'
  1. 启动 Dwarf Fortress + DFHack (./dfhack 或 Steam)
  2. 在游戏中按 ` 打开 DFHack 控制台, 执行:
        fanyi status      → 应显示引擎在线/离线 + 统计
        fanyi start       → 开启捕获
        fanyi overlays on → (字体贴图就绪后)开启 CJK 悬浮
  3. 触发公告(如让矮人迁移工作), 观察引擎日志收到 /translate
  4. 关闭引擎桥(kill <engine pid>)再触发公告 → 游戏不受影响, 显示原文;
     重启引擎桥 → ~15s 内自动重连(fanyi status 可见 reconnect 计数)
EOF
fi

# 4. 引擎侧自检(离线场景也随手报一次)
say "引擎侧自检"
if [[ "$NO_ENGINE" != "0" ]]; then
    echo "  (--no-engine: 跳过)"
else
    python3 - <<EOF
import json, socket
s = socket.create_connection(("$HOST", $PORT), timeout=2)
s.sendall((json.dumps({"jsonrpc": "2.0", "id": "probe-1", "method": "health", "params": {}}) + "\n").encode())
print("  health:", s.recv(4096).decode().strip())
s.close()
EOF
fi

say "探针完成"
exit 0