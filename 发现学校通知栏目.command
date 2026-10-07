#!/bin/bash
# 批量找各推免资格高校的「本科生院/教务处」「研究生院/研招」通知栏目，结果缓存到
# open_box/data/archive_cache.json，报告在 open_box/out/discover_report_<时间>.md。
# 只读学校官网，不花搜索额度；约 15–30 分钟。中途关掉再双击会跳过已完成的学校。
cd "$(dirname "$0")" || exit 1
REPO="$(pwd)"
PY="$REPO/.venv/bin/python"
if [ ! -x "$PY" ] || ! "$PY" -c 'import httpx, bs4' >/dev/null 2>&1; then
  echo "没有可用的 .venv（需要 httpx、beautifulsoup4）：先双击「启动网站.command」装好依赖。"
  read -r -p "按回车关闭"; exit 1
fi
# 用系统证书校验（补齐缺中间证书的学校官网），没装就装一次。
if ! "$PY" -c 'import truststore' >/dev/null 2>&1; then
  "$REPO/.venv/bin/pip" install -q truststore || echo "truststore 安装失败，继续用默认证书校验"
fi
mkdir -p "$REPO/open_box/out"
"$PY" "$REPO/open_box/discover_schools.py" "$@" 2>&1 | tee "$REPO/open_box/out/discover_latest.log"
echo
read -r -p "跑完了，按回车关闭"
