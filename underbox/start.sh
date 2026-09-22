#!/usr/bin/env bash
# underbox 一键启动。
# 用法：./start.sh [端口]     默认 8787，浏览器打开 http://127.0.0.1:8787/
#
# 依赖与密钥都在 open_box/.env（DeepSeek + 智谱搜索），无需另外设置。
# open_box 的代码路径可用环境变量 OPEN_BOX_ROOT 覆盖，默认 ../open_box 相对本目录。

set -e
cd "$(dirname "$0")"

PORT="${1:-8787}"
ROOT="${OPEN_BOX_ROOT:-$(cd .. && pwd)/open_box}"
if [ ! -d "$ROOT" ]; then
  echo "找不到 open_box 代码目录：$ROOT" >&2
  echo "用 OPEN_BOX_ROOT=/path/to/open_box ./start.sh 指定。" >&2
  exit 1
fi
export OPEN_BOX_ROOT="$ROOT"

# 端口被占用 → 多半是上一次的 serve.py 还挂在后台。自动结束它再启动，
# 免得用户面对一屏 "Address already in use" 的堆栈。
PIDS="$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "$PIDS" ]; then
  echo "端口 $PORT 被占用（PID $(echo "$PIDS" | tr '\n' ' ')，应是上次的 serve.py），自动结束…"
  echo "$PIDS" | xargs kill 2>/dev/null || true
  sleep 1.5
fi

# 解释器必须真的装了 httpx / pydantic 并就地验一遍。
# serve.py 是在**进程内** `from app.pipeline import run_pipeline` 的，选错解释器不会
# 立刻报错——页面照常打开、健康检查照常通过，等到上传简历那一刻才炸
# `ModuleNotFoundError`。所以这里逐个候选真跑一次 import，不只是看文件在不在。
# 顺序：项目自带 venv → WorkBuddy 内置环境 → 系统 python3。
PY=""
for c in "$(cd .. && pwd)/.venv/bin/python" \
         "$HOME/.workbuddy/binaries/python/envs/default/bin/python" \
         "$(command -v python3 || true)"; do
  if [ -n "$c" ] && [ -x "$c" ] && "$c" -c 'import httpx, pydantic' >/dev/null 2>&1; then
    PY="$c"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "找不到装了 httpx / pydantic 的 Python。先建一个：" >&2
  echo "  cd $(cd .. && pwd) && python3 -m venv .venv && .venv/bin/pip install -U pydantic httpx pypdf" >&2
  exit 1
fi

echo "underbox  http://127.0.0.1:$PORT/"
echo "  open_box $ROOT"
echo "  python   $PY"
echo "  停止：Ctrl-C"
exec "$PY" serve.py --port "$PORT"
