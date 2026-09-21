#!/usr/bin/env bash
# 把本地开发用的 open_box 同步进本仓库（排除密钥与真实简历样例）。
# 仓库里的 open_box/ 是它的快照——改了那边就跑一次本脚本，别在两边各改一份。
set -e
SRC="${1:-/Users/a1234/Desktop/open_box}"
DST="$(cd "$(dirname "$0")" && pwd)/open_box"
[ -d "$SRC" ] || { echo "源目录不存在：$SRC"; exit 1; }
rsync -a --delete \
  --exclude='.env' --exclude='CV_example/' --exclude='out/' \
  --exclude='__pycache__/' --exclude='.DS_Store' --exclude='*.pyc' --exclude='.pytest_cache/' \
  "$SRC/" "$DST/"
echo "已同步 $SRC → $DST"
