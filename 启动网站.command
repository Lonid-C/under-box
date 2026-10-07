#!/bin/bash
# 双击运行：首次会建 .venv 并装依赖，之后直接启动 underbox，并自动打开浏览器。
cd "$(dirname "$0")" || exit 1
REPO="$(pwd)"

if [ ! -x "$REPO/.venv/bin/python" ] || ! "$REPO/.venv/bin/python" -c 'import httpx, pydantic' >/dev/null 2>&1; then
  echo "首次运行：创建 Python 环境并安装依赖（约 1 分钟）…"
  python3 -m venv "$REPO/.venv" || { echo "创建 venv 失败：请先安装 Python 3（python.org 或 brew install python）"; read -r -p "按回车关闭"; exit 1; }
  "$REPO/.venv/bin/pip" install -q -U pip
  "$REPO/.venv/bin/pip" install -q -U pydantic httpx pypdf pdfminer.six python-docx beautifulsoup4 trafilatura \
    || { echo "依赖安装失败，见上方报错"; read -r -p "按回车关闭"; exit 1; }
fi

# 用系统证书校验（补齐缺中间证书的学校官网），没装就装一次。
if ! "$REPO/.venv/bin/python" -c 'import truststore' >/dev/null 2>&1; then
  "$REPO/.venv/bin/pip" install -q truststore || echo "truststore 安装失败，继续用默认证书校验"
fi

if [ ! -f "$REPO/open_box/.env" ]; then
  echo "提示：还没有 open_box/.env。页面能打开，但要跑真实核验，"
  echo "      请把 open_box/.env.example 复制成 .env，填上 DEEPSEEK_API_KEY 和 SEARCH_API_KEY 后重新双击。"
fi

( sleep 2; open "http://127.0.0.1:8787/" ) &
exec "$REPO/underbox/start.sh" 8787
