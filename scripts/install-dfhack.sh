#!/usr/bin/env bash
# install-dfhack.sh — 把 DF-FanYi 的 fanyi.lua 桥安装进 DFHack hack/scripts/。
#
# 用法:
#   scripts/install-dfhack.sh [-d <DF根目录>] [--check]
# 默认 DF 根目录: ~/Games/DwarfFortress (可用 DF_ROOT 环境变量覆盖)
# --check: 只检查安装状态, 不写入。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DF_ROOT="${DF_ROOT:-${1:-$HOME/Games/DwarfFortress}}"
MODE="${2:-install}"
if [[ "$MODE" == "-d" || "$MODE" == "--df-root" ]]; then MODE="install"; fi

SRC="$ROOT/dfhack/scripts/fanyi.lua"
DEST="$DF_ROOT/hack/scripts/fanyi.lua"

if [[ ! -f "$SRC" ]]; then
    echo "✗ 仓库脚本缺失: $SRC" >&2
    exit 2
fi

echo "== DFHack fanyi.lua 安装检查 =="
echo "  DF 根目录 : $DF_ROOT"

if [[ ! -d "$DF_ROOT/hack/scripts" ]]; then
    echo "  hack/scripts 目录不存在 —— 请确认该目录是 DFHack 安装" >&2
    exit 2
fi

if [[ -f "$DEST" ]] && diff -q "$SRC" "$DEST" >/dev/null 2>&1; then
    echo "  已安装且与仓库一致 (skip)"
    exit 0
fi

if [[ "$MODE" != "install" ]]; then
    echo "  未安装或版本不一致 (需要 install)"
    exit 1
fi

cp "$SRC" "$DEST"
echo "  ✓ fanyi.lua → $DEST"

# 验证: game 进程是否存在(DFHack 内置 Lua 语法检查只能在游戏内做, 这里做外部近似)
if command -v luac5.4 >/dev/null 2>&1; then
    luac5.4 -p "$DEST" && echo "  ✓ luac 语法检查通过"
elif command -v luajit >/dev/null 2>&1; then
    luajit -bl "$DEST" >/dev/null && echo "  ✓ luajit 语法检查通过"
fi

if pgrep -x dwarfort >/dev/null 2>&1; then
    echo "  提示: 游戏可能正在运行 —— 在游戏中执行: fanyi start && fanyi status"
    echo "        (或运行 scripts/dfhack-load-report.sh 自动探针)"
fi
echo "  完成。"