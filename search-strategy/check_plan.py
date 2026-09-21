#!/usr/bin/env python3
"""对照检查：旧模板 vs 新策略引擎。

用法：
    PYTHONPATH=/path/to/open_box python check_plan.py  [--profile student|professional|researcher|fallback]

打印每条陈述命中的档案、展开出的查询（含轮次/权重/域限定/意图），以及被预算裁掉的。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

OPEN_BOX = Path(os.environ.get("OPEN_BOX_ROOT", "/Users/a1234/Desktop/open_box"))
sys.path.insert(0, str(OPEN_BOX))

from app.plan import plan_with_notes                        # noqa: E402
from app.schema import Claim                                # noqa: E402
from app.strategy import ResumeProfile                      # noqa: E402

FIXTURE = OPEN_BOX / "fixtures" / "report_lin.json"

PROFILES = {
    "student": ResumeProfile(identity="student", level="entry",
                             industries=[], skills=["python", "c++"]),
    "professional": ResumeProfile(identity="professional", level="mid",
                                  industries=["internet"], skills=["python", "kubernetes"]),
    "researcher": ResumeProfile(identity="researcher", level="senior",
                                industries=["education"], skills=["nlp"]),
    "fallback": None,
}

ROUND_NAME = {1: "定锚", 2: "取证", 3: "补漏"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="student", choices=list(PROFILES))
    ap.add_argument("--verbose", "-v", action="store_true", help="打印每条查询的意图")
    ap.add_argument("--claim", default=None, help="只看某一条，如 c05")
    args = ap.parse_args()

    report = json.loads(FIXTURE.read_text(encoding="utf-8"))
    name = report["candidate_name"]
    prof = PROFILES[args.profile]
    resume_cats = [c["claim"]["category"] for c in report["claims"]]

    print(f"候选人 {name}　画像 {args.profile}"
          f"{'（走兜底档案）' if prof is None else f' identity={prof.identity} level={prof.level}'}")
    print(f"简历里出现的类别：{'、'.join(dict.fromkeys(resume_cats))}")
    print("─" * 78)

    total = 0
    for vc in report["claims"]:
        claim = Claim(**vc["claim"])
        if args.claim and claim.id != args.claim:
            continue
        plan = plan_with_notes(claim, name, prof, resume_cats)
        total += len(plan.queries)
        print(f"\n[{claim.id}] {claim.category}　要素 {len(claim.elements)} 个"
              f"　→ {len(plan.queries)} 条查询　档案 {plan.profile_id}（{plan.profile_name}）")
        for q in plan.queries:
            bits = [f"r{q.round}", ROUND_NAME.get(q.round, "?"), f"w{q.weight}"]
            if q.kind != "web":
                bits.append(f"[{q.kind}]")
            if q.site:
                bits.append(f"site={q.site}")
            bits.append(f"→「{q.element}」" if q.element else "→ 全要素")
            print(f"   {'　'.join(bits)}")
            print(f"      {q.text}")
            if args.verbose and q.purpose:
                print(f"      意图：{q.purpose}")
        for n in plan.notes:
            print(f"   · {n}")
        if plan.dropped:
            print(f"   · 预算裁掉 {len(plan.dropped)} 条：{plan.dropped[0]} …")

    print("\n" + "─" * 78)
    print(f"合计 {total} 条查询（旧模板口径：每条 2–5 条、上限 6）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
