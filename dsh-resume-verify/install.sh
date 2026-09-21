#!/usr/bin/env bash
# 把 dsh-resume-verify 装进一个 dsh profile。
#
# 为什么要这个脚本：dsh 用绝对路径加载插件时，模块解析的起点是**插件文件自己所在的位置**。
# 插件 import 的 @deepseek-ai/dsh-tools 等包只存在于 harness 的模块树里，
# 所以插件必须放在能向上走到那棵树的目录下，否则整个 boot 会失败并报
#   ERR_MODULE_NOT_FOUND: Cannot find package '@deepseek-ai/dsh-tools'
# 本脚本把插件放进 $DSH_HOME/profiles/ 下（那里有 harness 的 node_modules），
# 并按实际路径生成 patch，省掉手工改三处路径。
#
# 用法：
#   ./install.sh                                   # 自动探测 open_box 与 python
#   OPEN_BOX_ROOT=/path/to/open_box \
#   PYTHON_BIN=/path/to/python \
#   DSH_HOME=/path/to/dsh-home ./install.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPEN_BOX_ROOT="${OPEN_BOX_ROOT:-/Users/a1234/Desktop/open_box}"
PYTHON_BIN="${PYTHON_BIN:-}"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
TARGET_NAME="${TARGET_NAME:-resume-verify}"

if [ ! -d "$OPEN_BOX_ROOT/app" ]; then
  echo "找不到 open_box：$OPEN_BOX_ROOT（应包含 app/ 目录）。用 OPEN_BOX_ROOT 指定。" >&2
  exit 1
fi

# 没显式给 pythonBin 就按优先级探测：open_box 自带的 venv 最可靠
if [ -z "$PYTHON_BIN" ]; then
  for cand in "$OPEN_BOX_ROOT/.venv/bin/python" "$OPEN_BOX_ROOT/venv/bin/python"; do
    [ -x "$cand" ] && PYTHON_BIN="$cand" && break
  done
fi
if [ -z "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [ -z "$PYTHON_BIN" ]; then
  echo "找不到 Python。用 PYTHON_BIN 指定一个装了 open_box 依赖的解释器。" >&2
  exit 1
fi

# 规则层要 pydantic，PDF 还要 pdfminer.six——提前验，别等模型调用时才炸
if ! "$PYTHON_BIN" -c "import pydantic" 2>/dev/null; then
  echo "警告：$PYTHON_BIN 里没有 pydantic，工具会在调用时报错。" >&2
  echo "      建议：cd $OPEN_BOX_ROOT && python3 -m venv .venv && .venv/bin/pip install pydantic pdfminer.six python-docx beautifulsoup4 lxml" >&2
fi

if [ ! -d "$DSH_HOME/profiles" ]; then
  echo "找不到 $DSH_HOME/profiles。先初始化一个 profile，例如：" >&2
  echo "  DSH_HOME=$DSH_HOME dsh --profile web --dump-default-config > /dev/null" >&2
  exit 1
fi

TARGET="$DSH_HOME/profiles/$TARGET_NAME"
rm -rf "$TARGET"
mkdir -p "$TARGET"
cp -R "$HERE/src" "$HERE/python" "$TARGET/"

PATCH="$TARGET/cordis.yml"
cat > "$PATCH" <<YAML
# 由 install.sh 生成，勿手改（重跑脚本会覆盖）
- insert:
    - id: $TARGET_NAME
      name: '$TARGET/src/index.ts'
      config:
        openBoxRoot: '$OPEN_BOX_ROOT'
        pythonBin: '$PYTHON_BIN'
        timeoutMs: 60000
YAML

cat <<INFO

已安装到 $TARGET

启动（应先看到一行 "[resume-verify] 已注册 6 个工具：…"，看不到就是没装上）：

  DSH_HOME=$DSH_HOME dsh web --patch $PATCH

注意：插件路径必须是绝对路径，且插件必须留在能解析到 @deepseek-ai/* 的目录树内。
把 $TARGET 挪走或直接指向本仓库目录都会导致 boot 失败。
INFO
