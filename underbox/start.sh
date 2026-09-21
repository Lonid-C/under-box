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
[ -d "$ROOT" ] || ROOT="/Users/a1234/Desktop/open_box"
export OPEN_BOX_ROOT="$ROOT"

# 端口被占用 → 多半是上一次的 serve.py 还挂在后台。自动结束它再启动，
# 免得用户面对一屏 "Address already in use" 的堆栈。
PIDS="$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "$PIDS" ]; then
  echo "端口 $PORT 被占用（PID $(echo "$PIDS" | tr '\n' ' ')，应是上次的 serve.py），自动结束…"
  echo "$PIDS" | xargs kill 2>/dev/null || true
  sleep 1.5
fi

# 优先用带依赖的虚拟环境（httpx / pydantic 装在那里），没有就退回系统 python3
PY="$HOME/.workbuddy/binaries/python/envs/default/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

echo "underbox  http://127.0.0.1:$PORT/"
echo "  open_box $ROOT"
echo "  python   $PY"
echo "  停止：Ctrl-C"
exec "$PY" serve.py --port "$PORT"
