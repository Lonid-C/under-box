#!/usr/bin/env python3
"""实测智谱各搜索引擎：返回条数、link 填充率、域过滤是否生效。

为什么需要这个脚本：app/search.py 里有一条 2026-09-20 的实测结论——
「search_std / search_pro 的裸查询返回的 link 全是空的，只有 search_pro_sogou 带 URL」。
这条结论决定了裸查询必须走最贵的一档（0.05/次 vs 0.01），所以值得定期复验：
供应商改一次返回体，我们就能省下 40%。

用法（在能联网的机器上跑，会真实消耗额度）：

    cd ~/Downloads/under-box
    python3 search-strategy/probe_engines.py "全国大学生数学建模竞赛 获奖名单"
    python3 search-strategy/probe_engines.py "保研公示" --site tsinghua.edu.cn

单价（open.bigmodel.cn/pricing，2026-09 核）：
    search_std 0.01 · search_pro 0.03 · search_pro_sogou 0.05 · search_pro_quark 未列价
跑一次默认三档 ≈ 0.09 元。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/web_search"
PRICE = {"search_std": 0.01, "search_pro": 0.03,
         "search_pro_sogou": 0.05, "search_pro_quark": None}


def load_key() -> str:
    """优先环境变量，其次 open_box/.env——不把 key 写进任何输出。"""
    key = os.environ.get("SEARCH_API_KEY", "").strip()
    if key:
        return key
    env = ROOT / "open_box" / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("SEARCH_API_KEY=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip()
    sys.exit("没找到 SEARCH_API_KEY：设环境变量，或填进 open_box/.env")


def probe(key: str, query: str, engine: str, site: str | None) -> dict:
    import httpx
    body: dict = {"search_query": query, "search_intent": False, "search_engine": engine}
    if site:
        body["search_domain_filter"] = site
    try:
        r = httpx.post(ENDPOINT, json=body, timeout=30.0,
                       headers={"Authorization": f"Bearer {key}",
                                "Content-Type": "application/json"})
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
    items = r.json().get("search_result") or []
    linked = [it for it in items if (it.get("link") or it.get("url"))]
    in_domain = [it for it in linked
                 if site and site in (it.get("link") or it.get("url") or "")]
    return {"total": len(items), "linked": len(linked),
            "in_domain": len(in_domain) if site else None,
            "samples": [{"title": (it.get("title") or "")[:40],
                         "link": (it.get("link") or it.get("url") or "") or "（空）"}
                        for it in items[:3]]}


def main() -> int:
    ap = argparse.ArgumentParser(description="实测智谱搜索引擎的返回质量")
    ap.add_argument("query", help="要试的查询串")
    ap.add_argument("--site", default=None, help="域限定，不给就是裸查询")
    ap.add_argument("--engines", default="search_std,search_pro,search_pro_sogou",
                    help="逗号分隔，默认三档都试")
    args = ap.parse_args()

    key = load_key()
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    mode = f"域限定 {args.site}" if args.site else "裸查询（无域限定）"
    print(f"查询：{args.query}\n模式：{mode}\n" + "─" * 64)

    spend = 0.0
    results: dict[str, dict] = {}
    for engine in engines:
        got = probe(key, args.query, engine, args.site)
        results[engine] = got
        price = PRICE.get(engine)
        if price:
            spend += price
        tag = f"{price:.2f}元/次" if price else "未列价"
        if "error" in got:
            print(f"\n{engine}（{tag}）→ 失败：{got['error']}")
            continue
        rate = f"{got['linked']}/{got['total']}" if got["total"] else "0/0"
        line = f"\n{engine}（{tag}）→ {got['total']} 条，其中带 link {rate}"
        if got["in_domain"] is not None:
            line += f"，落在 {args.site} 内 {got['in_domain']} 条"
        print(line)
        for sm in got["samples"]:
            print(f"    · {sm['title'] or '（无标题）'}  {sm['link']}")

    print("\n" + "─" * 64)
    print(f"本次花费约 {spend:.2f} 元")

    # 结论：直接回答"能不能离开搜狗"这一个问题
    if not args.site:
        cheap = [e for e in ("search_std", "search_pro")
                 if results.get(e, {}).get("linked", 0) > 0]
        if cheap:
            print(f"✅ {'、'.join(cheap)} 的裸查询也返回了 link——"
                  f"可以把 SEARCH_ENGINE_OPEN 换成 {cheap[0]}，裸查询立省 "
                  f"{(0.05 - PRICE[cheap[0]]) / 0.05:.0%}。")
        else:
            print("❌ 便宜档裸查询仍然不返回 link，search.py 里那条实测结论依旧成立，"
                  "SEARCH_ENGINE_OPEN 保持 search_pro_sogou。")
    else:
        ok = [e for e, g in results.items()
              if g.get("in_domain") and g["in_domain"] == g.get("linked")]
        if ok:
            print(f"✅ 域过滤生效的引擎：{'、'.join(ok)}")
        print("对比各档的条数，就能看出 std 的召回比 pro 差多少——差得不多就值得省这 2/3。")
    print("\n（本脚本只读不写，不会改任何配置；key 不会出现在输出里。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
