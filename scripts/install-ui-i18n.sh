#!/usr/bin/env bash
# ============================================================================
# 矮人要塞 UI 实时汉化(dfint Lite 自编译)一键安装
#
# 原理: DFHack 的 dfhooks 链加载器自动加载游戏根目录所有 libdfhooks_*.so 并
#       转发 dfhooks API —— 把 dfint 编译产物按此命名放入即与 DFHack 并存,
#       零替换零冲突(DF-FanYi 公告桥照常工作)。
#
# 用法: bash scripts/install-ui-i18n.sh [游戏目录, 默认 ~/Games/DwarfFortress]
# 回滚: 删游戏根目录的 libdfhooks_dfint.so 和 dfint-data/ 即可
# 详见: docs/audits/UI_I18N_GUIDE.md
# ============================================================================
set -euo pipefail

GAME_DIR="${1:-$HOME/Games/DwarfFortress}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

[ -d "$GAME_DIR" ] || { echo "✗ 游戏目录不存在: $GAME_DIR"; exit 1; }
[ -d "$GAME_DIR/hack" ] && echo "✓ 检测到 DFHack(公告桥可共存)" || echo "⚠ 未发现 hack/, DFHack 缺失(仅 UI 汉化可用)"

# ---------- 0) 备份 ----------
B="$GAME_DIR/.backup-before-dfint-$(date +%Y%m%d-%H%M)"
mkdir -p "$B"
cp "$GAME_DIR/dfhooks_dfhack.ini" "$GAME_DIR/libdfhooks.so" "$B/" 2>/dev/null || true
echo "✓ 备份: $B"

# ---------- 1) 源码 + 编译(需 rustup nightly) ----------
echo "==> 克隆 dfint lite 分支源码..."
git clone --depth 1 -b lite https://gitee.com/vizv/dfint-rust-cjk.git "$WORK/dfint"

if ! rustc --version 2>/dev/null | grep -q nightly; then
  if ! command -v rustup >/dev/null 2>&1; then
    echo "==> 安装 rustup(用户级, 无需 sudo)..."
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain nightly --profile minimal
    . "$HOME/.cargo/env"
  else
    rustup toolchain install nightly --profile minimal
  fi
fi
echo "==> 编译(release, 约 1 分钟)..."
(cd "$WORK/dfint" && cargo build --release)
[ -f "$WORK/dfint/target/release/libdfint_hook.so" ] || { echo "✗ 编译产物缺失"; exit 1; }

# ---------- 2) 组装 dfint-data ----------
echo "==> 克隆翻译数据仓库..."
git clone --depth 1 https://gitee.com/vizv/df-translations.git "$WORK/dict"
D="$WORK/dfint-data"
mkdir -p "$D/fonts" "$D/lookups" "$D/dictionaries"

cp "$WORK/dfint/data/offsets.txt" "$D/"

# 界面/帮助词条 → simple-dictionary.csv(程序必需, 缺文件会 panic)
python3 - "$WORK/dict/translations" "$D" <<'PYEOF'
import csv, os, sys
src, out = sys.argv[1], sys.argv[2]
rows, seen = [], set()
def add(text, tr):
    text, tr = (text or '').strip(), (tr or '').strip()
    if text and tr and text.lower() not in seen:
        seen.add(text.lower()); rows.append((text, tr))
for name, pairs in [
    ('interfaces.csv',    [('text', 'text_translation')]),
    ('help-texts.csv',    [('text', 'text_translation')]),
    ('help-documents.csv',[('title','title_translation'), ('document','document_translation')]),
]:
    with open(os.path.join(src, name), newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            for a, b in pairs: add(r.get(a), r.get(b))
with open(os.path.join(out, 'simple-dictionary.csv'), 'w', newline='', encoding='utf-8') as f:
    w = csv.writer(f); w.writerow(['text', 'translation']); w.writerows(rows)
print(f"  simple-dictionary 词条: {len(rows)}")
PYEOF

# 程序会无条件 open 的文件必须存在(空表头占位)
printf 'text,translation\r\n'            > "$D/legacy-dictionary.csv"
printf 'text,translation\r\n'            > "$D/user-dictionary.csv"
printf 'table,text,translation\r\n'      > "$D/user-lookup.csv"

cp "$WORK"/dict/lookups/*.csv      "$D/lookups/"
cp "$WORK"/dict/dictionaries/*.csv "$D/dictionaries/"

cat > "$D/config.txt" <<'CFG'
[LOG_LEVEL:Info]
[LOG_FILE:dfint-data/dfint-log.log]
[FONT_FILE:dfint-data/fonts/NotoSansMonoCJKsc-Bold.otf]
[USE_LEGACY_DICTIONARY:NO]
CFG

echo "==> 下载 Noto Sans Mono CJK 字体(约 16MB)..."
curl -sL --max-time 180 -o "$D/fonts/NotoSansMonoCJKsc-Bold.otf" \
  "https://github.com/notofonts/noto-cjk/raw/main/Sans/Mono/NotoSansMonoCJKsc-Bold.otf"
file "$D/fonts/NotoSansMonoCJKsc-Bold.otf" | grep -q "font\|OpenType" || { echo "✗ 字体下载异常"; exit 1; }

# ---------- 3) 安装(纯新增, 不动任何现有文件) ----------
echo "==> 安装到 $GAME_DIR ..."
cp "$WORK/dfint/target/release/libdfint_hook.so" "$GAME_DIR/libdfhooks_dfint.so"
rm -rf "$GAME_DIR/dfint-data"
cp -r "$D" "$GAME_DIR/dfint-data"

echo
echo "✅ 安装完成。启动游戏: cd $GAME_DIR && ./dfhack"
echo "   游戏内 Ctrl+F2 开关中文; 日志: dfint-data/dfint-log.log"
echo "   回滚: rm $GAME_DIR/libdfhooks_dfint.so && rm -rf $GAME_DIR/dfint-data"
