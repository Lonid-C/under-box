"""六步流水线编排（第 5 节）。

简历/个人主页
  → 1 解析与隐藏文字检测
  → 2 陈述拆分（LLM）
  → 3 检索计划（规则）
  → 4 检索与页面读取（受预算约束）
  → 5 证据提取（LLM）+ 身份判定 + 来源分级 + 状态判定（全部规则）
  → 6 澄清问题生成（LLM）
  → 报告 JSON
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from . import judge
from .collect import Budget, collect_for_claim
from .llm import LLM, build_llm
from .parse import parse_document
from .plan import MAX_PAGE_READS, MAX_SEARCHES, MAX_SECONDS, plan_with_notes
from .profile import derive_profile
from .questions import default_next_step, generate_question
from .schema import Report
from .search import PageFetcher, Searcher, build_searcher
from .split import split_claims

ROOT = Path(__file__).resolve().parent.parent


def load_mock_report(report_id: str = "lin") -> Report:
    """mock 模式：前端与 live 模式完全一致，只是流水线换成读取 fixture JSON。"""
    p = ROOT / "fixtures" / f"report_{report_id}.json"
    return Report(**json.loads(p.read_text(encoding="utf-8")))


def run_pipeline(
    resume_path: str | Path,
    *,
    candidate_name: str = "",
    position: str = "",
    report_id: str = "live",
    profile=None,
    llm: LLM | None = None,
    searcher: Searcher | None = None,
    budget_factory=None,
    fetcher: PageFetcher | None = None,
    progress=None,
) -> Report:
    """live 模式。judge.assess 是唯一的判定入口，与 fixture 走的是同一套规则。

    profile 是简历画像（身份/行业/层级/技能），决定用哪套检索策略。
    不给就走兜底档案——预算更低，因为不知道去哪找时广撒网只换噪音。
    """
    llm = llm or build_llm()
    searcher = searcher or build_searcher()
    fetcher = fetcher or PageFetcher()
    # 调用方显式给了预算工厂，就完全照它走（验收里就是用它把预算压到 1 次来跑断路场景）；
    # 没给的话才由**检索计划自己带预算**——档案说 8 次检索、执行层却仍用默认 6 的话，
    # 多出来的查询永远不会执行，差异化策略就成了装饰。
    explicit_budget = budget_factory is not None
    budget_factory = budget_factory or Budget
    say = progress or (lambda *a, **k: None)

    # 1 解析 + 输入风险检测
    say("[1/6] 解析与隐藏文字检测")
    doc = parse_document(resume_path)

    # 2 画像：模型读简历，判断这是哪类人 → 决定用哪套检索策略
    profile = profile or derive_profile(doc.safe_text, llm)
    say(f"    画像 identity={profile.identity} level={profile.level}"
        f" industries={profile.industries or ['—']} skills={len(profile.skills)} 项")

    # 3 陈述拆分（只送 scrub 过的安全文本）
    say("[2/6] 陈述拆分")
    claims = split_claims(doc.safe_text, llm)

    # 姓名：调用方给的 > 画像里的（模型读的原词）> 实体里的。都空才留空——
    # 之前这里直接落到"（未标注姓名）"，导致检索计划里的 `{name}` 查询全部废掉。
    name = candidate_name or profile.name or (claims[0].entities.get("name", "") if claims else "")

    verified = []
    # 档案匹配要用整份简历的类别集合，不能只看当前这一条——见 strategy._matches
    resume_categories = [c.category for c in claims]
    for i, claim in enumerate(claims, start=1):
        # 3 检索计划（策略档案驱动）
        plan = plan_with_notes(claim, name, profile, resume_categories)
        say(f"[3-5/6] ({i}/{len(claims)}) {claim.id} {claim.category}"
            f" · {plan.profile_id} · {len(plan.queries)} 条查询 / 预算 {plan.budget.get('searches')} 次")
        # 4 检索与页面读取
        if explicit_budget:
            budget = budget_factory()
        else:
            budget = budget_factory(
                max_searches=plan.budget.get("searches", MAX_SEARCHES),
                max_page_reads=plan.budget.get("reads", MAX_PAGE_READS),
                max_seconds=plan.budget.get("seconds", MAX_SECONDS),
            )
        evidences, exhausted = collect_for_claim(
            claim, name, searcher, llm,
            budget=budget, fetcher=fetcher, queries=plan.queries,
        )
        # 5 判定：全部规则
        vc = judge.assess(claim, evidences, search_exhausted=exhausted)
        vc.next_step = default_next_step(vc)
        # 6 澄清问题
        vc.question = generate_question(vc, llm)
        verified.append(vc)

    say("[6/6] 汇总报告")
    return Report(
        id=report_id,
        candidate_name=name or "（未标注姓名）",
        position=position or "（未标注岗位）",
        report_no=f"RV-{datetime.now():%Y%m%d}-{report_id}",
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        mode="live",
        source_file=str(resume_path),
        fictional=False,
        input_risks=doc.risks,
        claims=verified,
    )


def run(resume_path: str | Path | None = None, **kw) -> Report:
    """按 MODE 环境变量选择模式，默认 mock。"""
    if os.environ.get("MODE", "mock") != "live":
        return load_mock_report()
    if resume_path is None:
        raise SystemExit("live 模式需要提供简历路径")
    return run_pipeline(resume_path, **kw)
