#!/usr/bin/env python3
"""等价性验收：桥的判定结果必须与 open_box 原有的 fixture 结论逐条一致。

这是"套进 dsh 架构"之后最要紧的一条保证——判定层的规则没有因为搬家而漂移。

做法：把 fixtures/report_lin.json 里每条 claim 的证据剥掉由规则算出来的字段
（source_tier、identity_score），只留下模型本来就会提供的信息，重新走一遍
桥的 judge 命令，再跟 fixture 里的 status / best_tier 比对。

用法：
    PYTHONPATH=<open_box 根目录> python python/test_equivalence.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
DEFAULT_OPEN_BOX = Path(os.environ.get("OPEN_BOX_ROOT", "/Users/a1234/Desktop/open_box"))
FIXTURE = DEFAULT_OPEN_BOX / "fixtures" / "report_lin.json"
BRIDGE = HERE / "resume_rules.py"

WECHAT_SUBJECT_RE = re.compile(r"认证主体[：:]\s*([^）)）]+)")


def call(payload: dict) -> dict:
    """跑一次桥，一条命令一个进程——与 dsh 插件的调用方式完全一致。"""
    env = {**os.environ, "PYTHONPATH": str(DEFAULT_OPEN_BOX)}
    proc = subprocess.run(
        [sys.executable, str(BRIDGE)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True, text=True, env=env, timeout=60,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(f"桥异常退出 {proc.returncode}: {proc.stderr[:400]}")
    return json.loads(proc.stdout)


def reconstruct(ev: dict) -> dict:
    """把 fixture 里的证据还原成"模型能提供的样子"。

    规则算出来的 source_tier / identity_score 一律丢掉——桥会重算，
    重算的结果才是我们要验的东西。
    """
    out = {k: v for k, v in ev.items() if k not in ("source_tier", "identity_score")}
    host = urlparse(ev.get("url", "")).hostname or ""
    if host in ("mp.weixin.qq.com", "mp.weixin.example"):
        m = WECHAT_SUBJECT_RE.search(ev.get("publisher", ""))
        if m:
            # 认证主体是页面上写明的事实，模型能从页面读到
            out["wechat_verified_subject"] = m.group(1).strip()
    return out


def organizer_hosts() -> list[str]:
    """主办方域名是**部署输入**，不是规则算出来的，所以这里按 fixture 的定义原样提供。

    取权威来源 fixtures/build_fixture.py 的 ORGANIZER_HOSTS，避免手抄。
    """
    sys.path.insert(0, str(DEFAULT_OPEN_BOX))
    try:
        from fixtures.build_fixture import ORGANIZER_HOSTS  # type: ignore
        return sorted(ORGANIZER_HOSTS)
    except Exception:
        return ["icpc-qingchuan.example", "qcacm.example"]


def main() -> int:
    if not FIXTURE.is_file():
        print(f"找不到 fixture：{FIXTURE}", file=sys.stderr)
        print("用 OPEN_BOX_ROOT 指定 open_box 根目录。", file=sys.stderr)
        return 2

    report = json.loads(FIXTURE.read_text(encoding="utf-8"))
    org_hosts = organizer_hosts()
    rows, bad = [], []

    for vc in report["claims"]:
        cid = vc["claim"]["id"]
        payload = {
            "cmd": "judge",
            "claim": vc["claim"],
            "evidences": [reconstruct(e) for e in vc["evidence"]],
            "organizer_hosts": org_hosts,
        }
        got = call(payload)
        if not got.get("ok"):
            bad.append((cid, "桥报错", got.get("error", "")))
            continue

        new = got["data"]["verified_claim"]
        same_status = new["status"] == vc["status"]
        same_tier = new["best_tier"] == vc["best_tier"]
        rows.append((cid, vc["status"], new["status"], vc["best_tier"], new["best_tier"],
                     same_status and same_tier))
        if not (same_status and same_tier):
            bad.append((cid, f"{vc['status']}/{vc['best_tier']}",
                        f"{new['status']}/{new['best_tier']}"))

    print(f"{'id':<5}{'fixture':<14}{'桥重算':<14}{'一致':<6}")
    print("─" * 46)
    for cid, s0, s1, t0, t1, ok in rows:
        flag = "✓" if ok else "✗"
        print(f"{cid:<5}{f'{s0} / {t0}':<14}{f'{s1} / {t1}':<14}{flag:<6}")

    passed = sum(1 for r in rows if r[5])
    print("─" * 46)
    print(f"{passed}/{len(rows)} 条一致")

    if bad:
        print("\n不一致明细：")
        for cid, want, got in bad:
            print(f"  {cid}: 期望 {want}，得到 {got}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
