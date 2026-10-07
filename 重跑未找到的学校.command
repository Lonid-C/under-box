#!/bin/bash
# 只重跑上次没找到栏目、或首页打不开的学校；已找到的保留不动。
# 没通过的候选页会存到 open_box/out/discover_debug/，供对照改规则。
exec "$(dirname "$0")/发现学校通知栏目.command" --retry-failed --debug
