#!/usr/bin/env python3
"""只核验「学历」类陈述（保研/推免 + 本科就读）的实测脚本。

    python edu_probe.py ~/Downloads/某某简历.pdf

和网页端走同一套代码（解析 → 画像 → 拆分 → 检索计划 → 检索/回读 → 规则判定），
区别只有两点：
  1. 拆分后只保留 category == "学历" 的陈述，其余经历不检索、不花钱；
  2. 把每一次搜索、每一个回读页面的结果和失败原因都记下来，写进
     out/edu_probe_<时间>.json，方便逐条对照"为什么没搜到"。

本科学历按设计只走两条探针：学校官网搜姓名、微信公众号搜「学校 + 姓名」；
另外会把保研条目读到的本科学校官方名单（如推免资格名单）作为本科就读的关联佐证。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from app import judge  # noqa: E402  (导入 app 时会先读 .env)
from app.collect import Budget, collect_for_claim  # noqa: E402
from app.llm import build_llm  # noqa: E402
from app.parse import parse_document  # noqa: E402
from app.plan import plan_with_notes  # noqa: E402
from app.profile import derive_profile  # noqa: E402
from app.schema import STATUS_LABEL  # noqa: E402
from app.search import CachedSearcher, PageFetcher, build_searcher  # noqa: E402
from app.split import split_claims  # noqa: E402
from app.strategy import _is_push_text, _claim_search_blob, add_admission_source_hints  # noqa: E402

# 智谱单价（元/次，open.bigmodel.cn/pricing，2026-09）：带域名走 std，不带域名走搜狗档。
PRICE_SITE, PRICE_OPEN = 0.01, 0.05


class RecordingSearcher:
    def __init__(self, inner):
        self.inner = inner
        self.log: list[dict] = []

    def search(self, query, site=None):
        started = time.monotonic()
        try:
            hits = self.inner.search(query, site=site)
        except Exception as exc:
            self.log.append({"query": query, "site": site, "error": f"{type(exc).__name__}: {exc}"})
            raise
        self.log.append({
            "query": query, "site": site, "seconds": round(time.monotonic() - started, 1),
            "hits": [{"url": h.url, "title": h.title} for h in hits[:10]],
            "total": len(hits),
        })
        return hits


class RecordingFetcher:
    def __init__(self, inner: PageFetcher):
        self.inner = inner
        self.log: list[dict] = []

    @property
    def failures(self):
        return self.inner.failures

    def get(self, url):
        started = time.monotonic()
        text = self.inner.get(url)
        self.log.append({
            "url": url, "ok": bool(text), "seconds": round(time.monotonic() - started, 1),
            "chars": len(text or ""), "failure": None if text else self.inner.failures.get(url),
        })
        return text


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python edu_probe.py <简历.pdf|.docx|.md> [候选人姓名]")
        return 2
    resume = Path(sys.argv[1]).expanduser()
    if not resume.exists():
        print(f"找不到简历文件：{resume}")
        return 2
    forced_name = sys.argv[2] if len(sys.argv) > 2 else ""

    llm = build_llm()
    searcher = RecordingSearcher(build_searcher())
    fetcher = RecordingFetcher(PageFetcher())
    cached = CachedSearcher(searcher)

    t0 = time.monotonic()
    doc = parse_document(resume)
    print(f"[1] 解析完成：{doc.kind.upper()}，正文 {len(doc.safe_text)} 字")
    profile = derive_profile(doc.safe_text, llm)
    name = forced_name or profile.name
    print(f"[2] 画像 identity={profile.identity} level={profile.level}；姓名：{name or '（未识别）'}")
    claims = add_admission_source_hints(split_claims(doc.safe_text, llm))
    edu = [c for c in claims if c.category == "学历"]
    print(f"[3] 拆分出 {len(claims)} 条陈述，其中学历 {len(edu)} 条（其余不检索）：")
    for c in edu:
        kind = "保研/推免" if _is_push_text(_claim_search_blob(c)) else "普通学历"
        hint = (c.entities or {}).get("source_school_hint")
        print(f"    {c.id} [{kind}] {c.raw_text}"
              + (f"  ← 推荐方：{hint}" if hint else ""))
    if not edu:
        print("没有学历类陈述，结束。")
        return 1

    categories = [c.category for c in claims]
    verified, plans = [], {}
    school_domains: dict[str, str] = {}
    for c in edu:
        plan = plan_with_notes(c, name, profile, categories)
        plans[c.id] = plan
        print(f"\n[4] {c.id} 检索计划 · {plan.profile_id} · 预算 {plan.budget}")
        for i, q in enumerate(plan.queries, 1):
            print(f"    {i:>2}. {q.text}" + (f"  @{q.site}" if q.site else ""))
        before = len(searcher.log)
        budget = Budget(max_searches=plan.budget["searches"], max_page_reads=plan.budget["reads"],
                        max_seconds=plan.budget["seconds"])
        evidences, exhausted = collect_for_claim(
            c, name, cached, llm, budget=budget, fetcher=fetcher, queries=plan.queries,
            progress=print, school_domains=school_domains)
        vc = judge.assess(c, evidences, search_exhausted=exhausted)
        verified.append(vc)
        print(f"    → {STATUS_LABEL[vc.status]}；已证明 {vc.proved or '无'}；"
              f"本条实际搜索 {len(searcher.log) - before} 次，用时 {budget.elapsed():.0f} 秒")

    changed = judge.corroborate_enrollment(verified, name, school_domains)
    for idx in changed:
        print(f"\n[5] 关联佐证：{verified[idx].claim.id} 借用其他学历条目的官方名单，"
              f"状态 → {STATUS_LABEL[verified[idx].status]}")

    cost = sum(PRICE_SITE if e.get("site") else PRICE_OPEN
               for e in searcher.log if "error" not in e)
    print("\n================ 结果 ================")
    for vc in verified:
        print(f"{vc.claim.id} {vc.claim.raw_text}")
        print(f"   状态：{STATUS_LABEL[vc.status]}   已证明：{vc.proved or '无'}   未证明：{vc.unproved or '无'}")
        for ev in vc.evidence:
            print(f"   · [{ev.source_tier}] {ev.title[:60]}")
            print(f"     {ev.url}")
            print(f"     摘录：{ev.snippet[:120]}")
    reads_ok = sum(1 for r in fetcher.log if r["ok"])
    print(f"\n搜索 {len(searcher.log)} 次（约 {cost:.2f} 元），回读 {len(fetcher.log)} 页"
          f"（成功 {reads_ok}），总用时 {time.monotonic() - t0:.0f} 秒")

    out_dir = HERE / "out"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"edu_probe_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps({
        "resume": str(resume), "name": name,
        "claims": [vc.model_dump() for vc in verified],
        "plans": {cid: {"profile": p.profile_id, "budget": p.budget,
                        "queries": [{"text": q.text, "site": q.site} for q in p.queries],
                        "dropped": p.dropped, "notes": p.notes} for cid, p in plans.items()},
        "searches": searcher.log, "page_reads": fetcher.log,
        "estimated_search_cost_cny": round(cost, 2),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"明细已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
