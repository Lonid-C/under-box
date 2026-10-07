#!/bin/bash
# 只核验简历里的学历条目（保研/推免 + 本科就读），记录每次搜索和回读明细。
# 结果：open_box/out/edu_probe_<时间>.json，屏幕输出另存 open_box/out/edu_probe_latest.log
cd "$(dirname "$0")" || exit 1
REPO="$(pwd)"
PY="$REPO/.venv/bin/python"
if [ ! -x "$PY" ] || ! "$PY" -c 'import httpx, pydantic' >/dev/null 2>&1; then
  echo "没有可用的 .venv：先双击「启动网站.command」装好依赖。"
  read -r -p "按回车关闭"; exit 1
fi

# 简历路径：命令行参数 > 上次选过的（记在 open_box/out/.last_resume，不入库）> 弹窗选择。
# 不在脚本里写死具体简历文件名：仓库里不放真实候选人的任何信息。
mkdir -p "$REPO/open_box/out"
LAST="$REPO/open_box/out/.last_resume"
RESUME="${1:-$(cat "$LAST" 2>/dev/null)}"
if [ ! -f "$RESUME" ]; then
  RESUME="$(osascript -e 'POSIX path of (choose file with prompt "选择要核验学历的简历（PDF / DOCX）")' 2>/dev/null)"
fi
if [ -z "$RESUME" ] || [ ! -f "$RESUME" ]; then
  echo "没有选择简历，结束。"; read -r -p "按回车关闭"; exit 1
fi
printf '%s' "$RESUME" > "$LAST"

echo "简历：$RESUME"
echo "只检索学历条目；预计 2–5 分钟，搜索费用约几毛钱。"
echo
"$PY" "$REPO/open_box/edu_probe.py" "$RESUME" 2>&1 | tee "$REPO/open_box/out/edu_probe_latest.log"
echo
read -r -p "跑完了，按回车关闭"
