#!/usr/bin/env bash
# ============================================================================
# install-df-fanyi-omarchy.sh — DF-FanYi 汉化引擎一键部署(新 omarchy/Linux 机器)
#
# 用法(游戏目录内, root 普通用户均可; 无需 sudo, 只装到用户目录):
#   cd "/path/to/Dwarf Fortress"
#   bash install-df-fanyi-omarchy.sh
#
# 做四件事:
#   1. 定位/克隆 DF-FanYi 引擎(本目录下已有 ./DF-FanYi 则直接复用)
#   2. 建 venv 安装引擎 + 复制离线翻译包 engine.db(游戏目录自带的那个)
#   3. (可选) 若同目录有 ./model-router/(从另一台机器拷贝, 见下) → 一并部署 AI 路由
#      —— 没有也不影响: 引擎可纯离线工作, 漏网句子显示原文而已
#   4. 注册并启动 systemd user 服务 df-fanyi-bridge(开机自启)
#
# 关于 AI 模型(二选一, 想让"漏网长句"也实时中文化才需要):
#   A. 云端路由(快, 需 API key): 把 model-router 整个目录拷贝到游戏目录:
#        rsync -a --exclude env --exclude logs --exclude .venv \
#              ~/model-router/  "/path/to/Dwarf Fortress/model-router/"
#      脚本检测到 ./model-router/ 后自动部署; 部署完后按提示把 API key
#      写进 <游戏目录>/model-router/env(密钥不会进入任何 git 仓库)
#   B. 本地 ollama(免费离线): 见安装指南第七节方案 A; 把 DF-FanYi
#      config/default.yaml 的 local_llm.base_url 指向本地 ollama
#
# 安全: 本脚本只写当前用户目录(~/.config/systemd/user 等), 不 sudo;
#       API key 只入本地 env 文件, 绝不写入脚本/仓库/git。
# ============================================================================
set -euo pipefail

# ---- 常量/探测 --------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GAME_DIR="$SCRIPT_DIR"
DF_FANYI_DIR="$GAME_DIR/DF-FanYi"
VENV_DIR="$DF_FANYI_DIR/.venv"
BRIDGE_PORT=17486
ROUTER_PORT=8010
MODEL_ROUTER_DIR="$GAME_DIR/model-router"
LOG="$GAME_DIR/install-df-fanyi.log"

info()  { printf '\033[1;36m[DF-FanYi]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[警告]\033[0m %s\n' "$*"; }
die()   { printf '\033[1;31m[失败]\033[0m %s\n' "$*" >&2; exit 1; }

info "脚本目录 = $GAME_DIR"

# ---- 前置检查 ---------------------------------------------------------------
[ -f "$GAME_DIR/Dwarf Fortress.exe" ] || \
  die "没找到 Dwarf Fortress.exe —— 脚本必须放在游戏根目录(Dwarf Fortress.exe 同层)运行"

PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "未找到 python3(需要 ≥3.11; 另可: apt install python3 / pacman -S python)"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' \
  || die "python3 版本太低: $("$PY" --version 2>&1) —— 需要 ≥3.11"

GIT="$(command -v git || true)"
[ -n "$GIT" ] || die "未找到 git"

# ---- 1. 定位/克隆 DF-FanYi ---------------------------------------------------
if [ -d "$DF_FANYI_DIR/.git" ]; then
  info "复用现有引擎: $DF_FANYI_DIR"
  git -C "$DF_FANYI_DIR" pull --quiet || warn "git pull 失败(离线? 继续用现有代码)"
elif [ -d "$DF_FANYI_DIR" ]; then
  warn "$DF_FANYI_DIR 存在但无 .git —— 当作源码目录直接使用(若不像源码将报错)"
else
  info "克隆 DF-FanYi 引擎(首次 ~几秒)..."
  git clone --quiet --depth 1 https://github.com/stflj2022/DF-FanYi.git "$DF_FANYI_DIR" \
    || die "git clone 失败 —— 请检查网络/代理, 或用现有整包拷贝"
fi
[ -d "$DF_FANYI_DIR/df_fanyi" ] || die "引擎目录不完整(缺 df_fanyi/ 包)"

# ---- 2. venv + 安装 + 离线翻译包 --------------------------------------------
if [ ! -x "$VENV_DIR/bin/python" ]; then
  info "创建虚拟环境 $VENV_DIR ..."
  "$PY" -m venv "$VENV_DIR"
fi
info "安装引擎依赖(pyyaml, requests)..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -e "$DF_FANYI_DIR"
[ -d "$DF_FANYI_DIR/data" ] || mkdir -p "$DF_FANYI_DIR/data"
if [ -f "$GAME_DIR/DF-FanYi-离线翻译包/engine.db" ]; then
  cp -f "$GAME_DIR/DF-FanYi-离线翻译包/engine.db" "$DF_FANYI_DIR/data/engine.db"
  info "离线翻译包已复制 → $DF_FANYI_DIR/data/engine.db"
else
  warn "未找到 离线翻译包/engine.db(不影响引擎启动, 但命中率会低)"
fi

# ---- 3. (可选) model-router 部署 ---------------------------------------------
if [ -f "$MODEL_ROUTER_DIR/router.py" ]; then
  info "检测到 model-router: $MODEL_ROUTER_DIR —— 部署 AI 云端路由"
  MR_VENV="$MODEL_ROUTER_DIR/.venv"
  if [ ! -x "$MR_VENV/bin/python" ]; then
    "$PY" -m venv "$MR_VENV"
  fi
  "$MR_VENV/bin/pip" install --quiet --upgrade pip
  "$MR_VENV/bin/pip" install --quiet fastapi uvicorn httpx pyyaml
  if [ ! -f "$MODEL_ROUTER_DIR/env" ]; then
    warn "model-router/env 不存在 —— 手动创建它(把 API key 填进去):"
    warn "  cp $MODEL_ROUTER_DIR/env.example $MODEL_ROUTER_DIR/env (若有样例)"
    warn "  或: nano $MODEL_ROUTER_DIR/env  内容形如:"
    warn "       GLM_API_KEY=你的智谱key   # 或 SCNET_API_KEY / OPENROUTER_API_KEY"
    [ -f "$MODEL_ROUTER_DIR/env.example" ] || \
      printf 'GLM_API_KEY=\nSCNET_API_KEY=\nOPENROUTER_API_KEY=\n' > "$MODEL_ROUTER_DIR/env.example"
  fi
  info "注册 model-router systemd 服务..."
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/df-fanyi-router.service" <<EOF
[Unit]
Description=model-router - AI translation backend (port $ROUTER_PORT)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$MODEL_ROUTER_DIR
EnvironmentFile=$MODEL_ROUTER_DIR/env
ExecStart=$MR_VENV/bin/python $MODEL_ROUTER_DIR/router.py
Restart=always
RestartSec=5
StandardOutput=append:$MODEL_ROUTER_DIR/logs/router.log
StandardError=append:$MODEL_ROUTER_DIR/logs/router.log

[Install]
WantedBy=default.target
EOF
  mkdir -p "$MODEL_ROUTER_DIR/logs"
  systemctl --user daemon-reload
  systemctl --user enable --now df-fanyi-router || warn "router 服务启动失败 —— 检查 env 里的 key"
else
  info "未检测到 ./model-router/ —— 跳过云端路由(引擎可纯离线工作)"
  warn "想让漏网长句也实时中文化: 拷 model-router 目录过来后重跑本脚本, 或改用 ollama(指南第七节)"
fi

# ---- 4. 注册 bridge 服务 -----------------------------------------------------
info "注册 df-fanyi-bridge systemd 服务..."
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/df-fanyi-bridge.service" <<EOF
[Unit]
Description=DF-FanYi translation engine bridge (JSON-RPC over loopback TCP $BRIDGE_PORT)
After=network.target

[Service]
Type=simple
WorkingDirectory=$DF_FANYI_DIR
ExecStart=$VENV_DIR/bin/python -m df_fanyi bridge --transport tcp --port $BRIDGE_PORT
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload || die "systemctl --user 不可用 —— 你是 root? 该脚本只支持普通用户(systemd user 实例)"
systemctl --user enable --now df-fanyi-bridge || die "bridge 服务启动失败 —— 看: journalctl --user -u df-fanyi-bridge -n 50"

# ---- 5. 验证 ------------------------------------------------------------------
sleep 1
if systemctl --user is-active --quiet df-fanyi-bridge; then
  info "✔ 引擎桥已启动: 127.0.0.1:$BRIDGE_PORT(systemd: df-fanyi-bridge, 开机自启)"
else
  warn "engine bridge 状态异常 — 日志: journalctl --user -u df-fanyi-bridge -n 50"
fi
if systemctl --user is-active --quiet df-fanyi-router 2>/dev/null; then
  info "✔ AI 路由已启动: 127.0.0.1:$ROUTER_PORT(systemd: df-fanyi-router)"
else
  info "AI 路由未启用(可选) —— 不影响静态/引擎核心中文化"
fi

info "全部完成! 现在可以启动游戏了(Dwarf Fortress.exe 或 Steam + Proton)。"
info "验证字幕: 进游戏后 DFHack 控制台执行: fanyi status"
printf '\033[1;32m[完成]\033[0m 日志: %s\n' "$LOG"