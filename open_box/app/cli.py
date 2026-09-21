"""命令行：verify <简历路径>。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .llm import LLMUnavailable, NullLLM, build_llm
from .pipeline import load_mock_report, run_pipeline
from .search import NullSearcher, build_searcher
from .schema import STATUS_LABEL, Report


def cmd_verify(args: argparse.Namespace) -> int:
    mode = os.environ.get("MODE", "mock")
    if mode == "live":
        try:
            report = run_pipeline(
                args.resume,
                candidate_name=args.name or "",
                position=args.position or "",
                report_id=args.report_id,
                progress=lambda m: print(m, file=sys.stderr),
            )
        except LLMUnavailable as exc:
            print(f"\nlive 模式跑不起来：{exc}", file=sys.stderr)
            print("离线演示用 `make demo`，不需要任何 key。", file=sys.stderr)
            return 2
    else:
        print("MODE=mock：直接输出内置 fixture，未做任何真实检索。", file=sys.stderr)
        report = load_mock_report()

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")

    counts = report.counts()
    print(f"\n{report.candidate_name} · {len(report.claims)} 条陈述 · 模式 {report.mode}")
    for s, n in counts.items():
        print(f"  {STATUS_LABEL[s]:<8} {n}")
    if report.input_risks:
        print(f"  输入风险 {len(report.input_risks)} 处（已从送入模型的内容中剔除）")
    print(f"\n报告已写入 {out}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """在给定报告上实测检索覆盖率。数字是算出来的，不要手抄进文档。"""
    if args.report == "mock":
        report = load_mock_report()
    else:
        report = Report(**json.loads(Path(args.report).read_text(encoding="utf-8")))

    total = len(report.claims)
    with_any = sum(1 for v in report.claims if v.evidence)
    with_valid = sum(1 for v in report.claims if v.status in ("ok", "part", "ask"))
    fully = sum(1 for v in report.claims if v.status == "ok")
    elements = sum(len(v.claim.elements) for v in report.claims)
    proved = sum(len(v.proved) for v in report.claims)
    human = sum(1 for v in report.claims if v.needs_human)

    def pct(a, b):
        return f"{a}/{b}（{a / b * 100:.0f}%）" if b else "—"

    print(f"报告 {report.id} · 模式 {report.mode} · 共 {total} 条陈述")
    print(f"  检索到公开来源            {pct(with_any, total)}")
    print(f"  来源可关联到本人          {pct(with_valid, total)}")
    print(f"  要素被全部证明（已证实）  {pct(fully, total)}")
    print(f"  要素级覆盖率              {pct(proved, elements)}")
    print(f"  需人工复核                {pct(human, total)}")
    print("\n注：覆盖率只反映公开信息的多少，不代表候选人的可信程度。")
    if report.mode == "mock":
        print("注：以上为内置虚构 fixture 上的实测值，不是真实核验结果。")
    return 0


def cmd_llm_check(args: argparse.Namespace) -> int:
    """一次最小调用，验证 key / 端点 / 模型名能不能用。"""
    llm = build_llm(args.provider)
    if isinstance(llm, NullLLM):
        print("未检测到 API key。请先设置 DEEPSEEK_API_KEY（或 LLM_API_KEY）。", file=sys.stderr)
        return 2

    print(f"供应商 {llm.provider}\n端点   {llm.endpoint}\n模型   {llm.model}\n")
    started = time.monotonic()
    try:
        got = llm.ping()
    except Exception as exc:
        print(f"✗ 调用失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"✓ 调通了，耗时 {time.monotonic() - started:.1f}s，返回 {got}")
    print("\n接着可以跑：MODE=live python -m app.cli verify samples/resume_lin.pdf -o out/report.json")
    return 0


def cmd_search_check(args: argparse.Namespace) -> int:
    """一次最小检索，验证 key / 端点 / 引擎名，并确认 site 限定真的生效。"""
    s = build_searcher(args.provider)
    if isinstance(s, NullSearcher):
        print("未检测到 SEARCH_API_KEY。", file=sys.stderr)
        return 2

    print(f"供应商 {type(s).__name__}\n端点   {getattr(s, 'endpoint', '')}\n"
          f"引擎   {getattr(s, 'engine', '—')}\n")
    started = time.monotonic()
    try:
        hits = s.ping() if hasattr(s, "ping") else s.search("测试")
    except Exception as exc:
        print(f"✗ 检索失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"✓ 调通了，耗时 {time.monotonic() - started:.1f}s，返回 {len(hits)} 条")
    for h in hits[:3]:
        print(f"    {h.url}\n      {h.title[:60]}")
    if hits and not all(".edu.cn" in h.url for h in hits):
        print("\n注意：site 限定似乎没完全生效，结果里有站外链接。"
              "检查 search_domain_filter 的参数名是否随文档变了。")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.cli", description="履历核验 demo")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="核验一份简历")
    v.add_argument("resume", help="简历路径（pdf / docx / md / txt）")
    v.add_argument("-o", "--output", default="out/report.json")
    v.add_argument("--name", default="", help="候选人姓名（拆分结果里没有时用）")
    v.add_argument("--position", default="", help="应聘岗位")
    v.add_argument("--report-id", default="live")
    v.set_defaults(func=cmd_verify)

    st = sub.add_parser("stats", help="实测检索覆盖率")
    st.add_argument("report", nargs="?", default="mock", help='"mock" 或报告 JSON 路径')
    st.set_defaults(func=cmd_stats)

    lc = sub.add_parser("llm-check", help="验证 LLM key / 端点 / 模型名")
    lc.add_argument("--provider", default=None, help="默认读 LLM_PROVIDER，缺省为 deepseek")
    lc.set_defaults(func=cmd_llm_check)

    sc = sub.add_parser("search-check", help="验证搜索 key / 端点 / 引擎名")
    sc.add_argument("--provider", default=None, help="默认读 SEARCH_PROVIDER，缺省为 zhipu")
    sc.set_defaults(func=cmd_search_check)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
