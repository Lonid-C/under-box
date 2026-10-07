#!/usr/bin/env python3
"""批量发现各校通知栏目，写进 data/archive_cache.json，并出一份报告。

    python discover_schools.py                 # 所有推免资格高校，推荐方 + 接收方
    python discover_schools.py --only 北京大学 清华大学
    python discover_schools.py --refresh       # 忽略缓存重新发现
    python discover_schools.py --role source   # 只找本科生院/教务处
    python discover_schools.py --retry-failed --debug
                                               # 只重跑没找到的学校/角色，并把没通过的候选页
                                               # 存到 out/discover_debug/ 供人工对照改规则

已经有有效缓存的学校会跳过，所以中途关掉再跑会接着做。不花搜索额度：只读学校官网，
守 robots.txt，同一站点至少间隔 1 秒。报告在 out/discover_report_<时间>.md。
"""
from __future__ import annotations

import argparse
import json
import warnings
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from app import discover  # noqa: E402

warnings.filterwarnings("ignore", message=".*XML.*")
from app.plan import _load_schools, school_archives  # noqa: E402
from app.search import PageFetcher  # noqa: E402

ROLE_LABEL = {"source": "本科生院/教务处", "receiver": "研究生院/研招"}


def main() -> int:
    ap = argparse.ArgumentParser(description="批量发现学校通知栏目")
    ap.add_argument("--only", nargs="*", help="只跑这些学校")
    ap.add_argument("--role", choices=["source", "receiver", "both"], default="both")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存")
    ap.add_argument("--workers", type=int, default=12,
                    help="同时跑几所学校（每所内部还会并行探测 8 个子站；开太大反而大量连接超时）")
    ap.add_argument("--retry-failed", action="store_true", help="只重跑上次没找到的学校和角色")
    ap.add_argument("--debug", action="store_true", help="保存没通过的候选页到 out/discover_debug/")
    ap.add_argument("--limit", type=int, default=0, help="最多跑多少所（抽样诊断用）")
    args = ap.parse_args()
    if args.debug:
        discover.DEBUG_DIR = HERE / "out" / "discover_debug"

    schools = [s for s in _load_schools() if s.get("push_qualified") and not s.get("fictional")]
    if args.only:
        schools = [s for s in schools if s["name"] in args.only]
    roles = ["source", "receiver"] if args.role == "both" else [args.role]

    def needs_retry(school: dict, role: str) -> bool:
        if school_archives(school["name"], role):
            return False
        hit = discover.cached(school["name"], role)
        if hit is None or not hit.get("archives"):
            return True
        # 第一轮没核对站点名称，jwb. 可能是纪检监察网而不是教务部：这类成功也重查。
        first = hit["archives"][0].get("first_page", "")
        return "//jwb." in first and not hit.get("office_checked")

    if args.retry_failed:
        schools = [s for s in schools if any(needs_retry(s, r) for r in roles)]
    if args.limit:
        schools = schools[:args.limit]
    # 发现阶段只等 6 秒连接、不重试：不存在的子域名很多，原来每个白等 10 秒。
    # 第一轮连接超时的学校，最后用更长的超时、更低的并发再补跑一轮（见下面 slow pass）。
    fetcher = PageFetcher(timeout=10.0, connect_timeout=6.0)
    fetcher.PAGE_RETRIES = 0
    rows: list[dict] = []
    lock = threading.Lock()
    done = 0
    t0 = time.monotonic()

    def run(school: dict, fetcher: PageFetcher = fetcher, force: tuple[str, ...] = ()) -> dict:
        name, domain = school["name"], school.get("domain") or ""
        row = {"school": name, "domain": domain, "province": school.get("province", "")}
        if not domain:
            row["domain_check"] = {"ok": False, "reason": "表里没有域名"}
            return row
        row["domain_check"] = discover.check_domain(name, domain, fetcher.get, fetcher.failures)
        for role in roles:
            if school_archives(name, role):
                row[role] = {"status": "人工配置", "first_page": school_archives(name, role)[0]["first_page"]}
                continue
            hit = None if (args.refresh or role in force or (args.retry_failed and needs_retry(school, role))) \
                else discover.cached(name, role)
            if hit is not None:
                a = (hit.get("archives") or [{}])[0]
                row[role] = {"status": "缓存:" + ("成功" if hit.get("archives") else "失败"),
                             "first_page": a.get("first_page", ""), "reason": hit.get("reason", "")}
                continue
            # 首页打不开也照样试子域名：华工 www 握手失败、yz. 却能正常访问。
            entry = discover.discover(name, domain, role, fetcher.get, max_fetches=60,
                                      why=fetcher.failures.get)
            discover.save_entry(name, role, entry)
            a = (entry["archives"] or [{}])[0]
            row[role] = {"status": "成功" if entry["archives"] else "失败",
                         "first_page": a.get("first_page", ""), "order": a.get("page_order", ""),
                         "pages": a.get("total_pages_seen"), "oldest": a.get("oldest_seen", ""),
                         "reason": entry.get("reason", ""), "fetches": entry.get("fetches"),
                         "rejects": entry.get("rejects", [])[:4], "unreachable": entry.get("unreachable", {})}
        return row

    print(f"共 {len(schools)} 所学校，角色：{', '.join(ROLE_LABEL[r] for r in roles)}；并发 {args.workers}")
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(run, s): s["name"] for s in schools}
        for fut in as_completed(futures):
            try:
                row = fut.result()
            except Exception as exc:                     # 单校出错不影响整批
                row = {"school": futures[fut], "error": f"{type(exc).__name__}: {exc}"}
            with lock:
                rows.append(row)
                done += 1
                marks = " ".join(f"{ROLE_LABEL[r][:3]}:{row.get(r, {}).get('status', '-')}" for r in roles)
                dom = "域名✓" if row.get("domain_check", {}).get("ok") else \
                    f"域名✗({row.get('domain_check', {}).get('reason', row.get('error', ''))})"
                print(f"[{done}/{len(schools)}] {row['school']} {dom} {marks}", flush=True)

    # 慢速补跑：首页连接超时的学校，多半是本机到学校网络慢（境外出口、晚高峰），
    # 换 10 秒连接超时、6 所并发再试一次（不再重试单个页面）。被防火墙/robots 拦的、
    # 连接直接被拒的不重试——2026-10-07 实测这类补跑 14 所 0 所变好，只是白等。
    def slow(row: dict) -> bool:
        why = row.get("domain_check", {}).get("reason", "")
        return "连接超时" in why and any(row.get(r, {}).get("status") == "失败" for r in roles)

    again = [r for r in rows if slow(r)]
    if again:
        print(f"\n慢速补跑 {len(again)} 所（首页连接超时的，连接超时放宽到 10 秒、并发 6）……", flush=True)
        slow_fetcher = PageFetcher(timeout=15.0, connect_timeout=10.0)
        slow_fetcher.PAGE_RETRIES = 0
        by_name = {s["name"]: s for s in schools}
        args.retry_failed = False                         # 只重跑第一轮失败的角色，成功的直接读缓存
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(run, by_name[r["school"]], slow_fetcher,
                                   tuple(x for x in roles if r.get(x, {}).get("status") in ("失败", "跳过", None))):
                       r["school"] for r in again if r["school"] in by_name}
            for fut in as_completed(futures):
                try:
                    new = fut.result()
                except Exception as exc:
                    new = {"school": futures[fut], "error": f"{type(exc).__name__}: {exc}"}
                new["slow_pass"] = True
                with lock:
                    rows[:] = [x for x in rows if x["school"] != new["school"]] + [new]
                    marks = " ".join(f"{ROLE_LABEL[r][:3]}:{new.get(r, {}).get('status', '-')}" for r in roles)
                    print(f"[补跑] {new['school']} {'域名✓' if new.get('domain_check', {}).get('ok') else '域名✗'} {marks}",
                          flush=True)

    rows.sort(key=lambda r: (r.get("province", ""), r["school"]))
    out_dir = HERE / "out"
    out_dir.mkdir(exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
    (out_dir / f"discover_report_{stamp}.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    def ok(r, role):
        return r.get(role, {}).get("status", "") in ("成功", "人工配置", "缓存:成功")

    lines = [f"# 学校通知栏目发现报告（{stamp}）", "",
             f"本次跑了 {len(rows)} 所，用时 {time.monotonic() - t0:.0f} 秒。", ""]
    for role in roles:
        n = sum(ok(r, role) for r in rows)
        lines.append(f"- {ROLE_LABEL[role]}：找到 {n} 所，未找到 {len(rows) - n} 所")
    # 全部推免资格高校的累计覆盖（含以前跑成功、缓存里的）
    every = [s for s in _load_schools() if s.get("push_qualified") and not s.get("fictional")]
    lines += ["", f"累计（全部 {len(every)} 所推免资格高校，含缓存）："]
    for role in roles:
        n = sum(1 for s in every if school_archives(s["name"], role)
                or (discover.cached(s["name"], role) or {}).get("archives"))
        lines.append(f"- {ROLE_LABEL[role]}：{n} 所有可用栏目")

    def category(r: dict, role: str) -> str:
        why = str(r.get(role, {}).get("reason", "")) + " " + " ".join(
            x.get("reason", "") for x in r.get(role, {}).get("rejects", []))
        dom = r.get("domain_check", {}).get("reason", "")
        if "防火墙" in why + dom or "412" in why + dom:
            return "网站防火墙拦截（要浏览器执行脚本，程序不绕过）"
        if "robots" in why + dom:
            return "robots.txt 不允许"
        if "候选站点都打不开" in why or ("首页打不开" in dom and not r.get(role, {}).get("rejects")):
            return "连不上（多为境外网络访问慢/被限，建议在境内网络再跑）"
        if "可能要浏览器执行脚本" in why:
            return "列表是脚本加载的（静态页面里没有条目）"
        if "登录/管理系统" in why and "带日期" not in why:
            return "只找到登录系统"
        if "带日期的条目" in why or "翻页" in why or "第 2 页" in why:
            return "找到栏目但格式没认出来"
        return "没找到通知栏目"

    for role in roles:
        cats: dict[str, int] = {}
        for r in rows:
            if not ok(r, role):
                c = category(r, role)
                cats[c] = cats.get(c, 0) + 1
        if cats:
            lines += ["", f"{ROLE_LABEL[role]} 未找到的原因分布："]
            lines += [f"- {k}：{v}" for k, v in sorted(cats.items(), key=lambda kv: -kv[1])]
    bad_dom = [r for r in rows if not r.get("domain_check", {}).get("ok")]
    lines += ["", f"## 域名有问题（{len(bad_dom)}）", "", "| 学校 | 域名 | 原因 |", "|---|---|---|"]
    lines += [f"| {r['school']} | {r.get('domain')} | {r.get('domain_check', {}).get('reason', r.get('error', ''))} |"
              for r in bad_dom]
    for role in roles:
        miss = [r for r in rows if not ok(r, role)]
        lines += ["", f"## {ROLE_LABEL[role]} 未找到（{len(miss)}）", "", "| 学校 | 原因 |", "|---|---|"]
        lines += [f"| {r['school']} | {category(r, role)}：{r.get(role, {}).get('reason', r.get('error', ''))}"
                  + "".join(f"；{x['reason']}（{x['url']}）" for x in r.get(role, {}).get("rejects", [])[:2])
                  + " |" for r in miss]
    report = out_dir / f"discover_report_{stamp}.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:30]))
    print(f"\n报告：{report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
