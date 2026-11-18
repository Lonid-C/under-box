"""六步流水线编排（第 5 节）。

简历/个人主页
  → 1 解析与隐藏文字检测
  → 2 陈述拆分（LLM）
  → 3 检索计划（规则）
  → 4 检索与页面读取（受预算约束）
  → 5 证据提取（LLM）+ 身份判定 + 来源分级 + 状态判定（全部规则）
  → 6 澄清问题生成（LLM）
  → 报告 JSON

并行：画像与陈述拆分同时调用模型；依赖先行，同批独立陈述并行进行
检索、回读、判定和澄清问题生成（`CLAIM_WORKERS`，默认 0 = 同批全部开始；设 1 恢复
串行）。学历阶段及已选作品→任职/教学/艺术遵循协作计划，报告原顺序不变。
模型调用另有总并发上限（`LLM_CONCURRENCY`，默认 10），
搜索 API 由 search.py 统一错峰，学校官网仍按单域串行、间隔 1 秒，不会因为并行而加压。
"""
from __future__ import annotations
from .quota_policy import unlimited

import json
import os
import re
import threading
import time
from contextlib import nullcontext
from .usage import UsageExecutor as ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import judge
from . import ict_pipeline
from .collect import Budget, EVIDENCE_OBSERVER, collect_for_claim
from .llm import LLM, build_llm
from .competitions import (competition_for_claim, competition_resolution_note, lookup_summary,
                           publication_note, seed_pages)
from .parse import ParsedDocument, parse_document
from .plan import MAX_PAGE_READS, MAX_SEARCHES, MAX_SECONDS, plan_with_notes
from .profile import derive_profile
from .resume_gate import ensure_resume
from .questions import default_next_step, generate_question
from .source_guard import SourceGuardFetcher, SourceGuardSearcher, evidence_excluded, for_claim
from .employer_scope import employer_policy
from .schema import STATUS_LABEL, Report
from .search import CachedSearcher, PageFetcher, READ_DEADLINE, Searcher, build_searcher, read_remaining
from .split import split_claims
from .strategy import add_admission_source_hints, add_competition_school_hints
from .identity import with_candidate_identity_context
from .academic import verification_phase, with_verified_education_context, study_stage, AWARD_CATEGORIES
from .verification_agents import agent_for, VerificationCoordinator
from .internship import build_manual_review

ROOT = Path(__file__).resolve().parent.parent


def load_mock_report(report_id: str = "lin") -> Report:
    """mock 模式：前端与 live 模式完全一致，只是流水线换成读取 fixture JSON。"""
    p = ROOT / "fixtures" / f"report_{report_id}.json"
    return Report(**json.loads(p.read_text(encoding="utf-8")))


def claim_workers(n: int) -> int:
    """同时处理几条陈述。CLAIM_WORKERS=0/未设 → 全部同时开始；1 → 逐条串行（旧行为）。"""
    configured = os.environ.get("UNDERBOX_ICT_CLAIM_WORKERS") if ict_pipeline.parallel_enabled() else None
    try:
        w = int(configured or os.environ.get("CLAIM_WORKERS", "0") or 0)
    except ValueError:
        w = 0
    if w <= 0:
        w = n
    return max(1, min(w, n, 32))


# 并行时每条陈述的时间预算放宽的倍数：各条同时跑，总耗时取最慢的一条而不是逐条相加，
# 但共用搜索 API 的错峰和模型并发上限，单条会比串行时慢一些，给一点余量。
PARALLEL_TIME_FACTOR = 1.5


class BoundedLLM:
    """给模型调用加总并发上限（LLM_CONCURRENCY，默认 10）。其余属性原样透传。

    十几条陈述同时开始、每条又并发回读 3 页时，抽取调用能一下子冲到三十多个；
    限一下并发能避开供应商的 429，排队比被限流后退避重试更快。
    """

    def __init__(self, inner, limit: int):
        self._inner = inner
        self._sem = threading.BoundedSemaphore(max(1, limit))

    def complete_json(self, *a, **k):
        with self._sem:
            return self._inner.complete_json(*a, **k)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _llm_limit() -> int:
    try:
        return max(1, int(os.environ.get("LLM_CONCURRENCY", "10") or 10))
    except ValueError:
        return 10


def skip_categories() -> set[str]:
    """显式跳过的类别。默认实习进入公司联系方式/人工背调流程。
    SKIP_CATEGORIES 可改（逗号分隔；设为 - 表示都核验）。"""
    raw = os.environ.get("SKIP_CATEGORIES")
    if raw is None:
        raw = ""
    return {x.strip() for x in re.split(r"[,，、\s]+", raw) if x.strip() and x.strip() != "-"}


@dataclass
class PreparedResume:
    """拆分完、还没检索的一份简历：网页端先给用户看这些陈述、勾选要核验哪些，再检索。"""
    resume_path: str
    doc: ParsedDocument
    llm: object
    profile: object
    claims: list
    name: str
    skipped: list = None          # 按 SKIP_CATEGORIES 显式跳过的陈述

    def __post_init__(self):
        if self.skipped is None:
            self.skipped = []


def prepare_resume(
    resume_path: str | Path,
    *,
    candidate_name: str = "",
    profile=None,
    llm: LLM | None = None,
    progress=None,
    parsed_document: ParsedDocument | None = None,
    on_profile=None,
) -> PreparedResume:
    """第 1–2 步：解析、确认是简历、画像与陈述拆分。不做任何检索。"""
    # 解析必须先于画像、拆分和检索；网页端已经解析过时直接复用，避免 PDF 读两遍。
    doc = parsed_document if parsed_document is not None else parse_document(resume_path)
    llm = llm or build_llm()
    ensure_resume(doc, llm)
    say = progress or (lambda *a, **k: None)

    # 1 解析 + 输入风险检测
    say(f"[1/6] 已识别 {doc.kind.upper()} 且确认是简历，正文解析与隐藏文字检测完成")

    parallel = claim_workers(1 << 10) > 1          # CLAIM_WORKERS=1 时整条流水线保持串行
    if parallel:
        llm = BoundedLLM(llm, _llm_limit())

    def _split():
        split = ict_pipeline.split_resume if ict_pipeline.enabled() else split_claims
        return add_competition_school_hints(add_admission_source_hints(split(doc.safe_text, llm)))

    # 2 画像（决定用哪套检索策略）与 3 陈述拆分：两次互不依赖的模型调用，并行时同时发出
    if profile is None and parallel:
        say("[2/6] 画像归纳与陈述拆分同时进行")
        with ThreadPoolExecutor(max_workers=2) as pool:
            split_future = pool.submit(_split)
            profile = derive_profile(doc.safe_text, llm)
            if on_profile:
                on_profile(profile)
            claims = split_future.result()
        say(f"    画像 identity={profile.identity} level={profile.level}"
            f" industries={profile.industries or ['—']} skills={len(profile.skills)} 项")
    else:
        profile = profile or derive_profile(doc.safe_text, llm)
        if on_profile:
            on_profile(profile)
        say(f"    画像 identity={profile.identity} level={profile.level}"
            f" industries={profile.industries or ['—']} skills={len(profile.skills)} 项")
        say("[2/6] 陈述拆分")
        claims = _split()

    # 姓名：调用方给的 > 画像里的（模型读的原词）> 实体里的。都空才留空——
    # 之前这里直接落到"（未标注姓名）"，导致检索计划里的 `{name}` 查询全部废掉。
    from .split import LAST_SPLIT
    if LAST_SPLIT.get("dropped") or not claims:
        dropped = LAST_SPLIT.get("dropped") or []
        say(f"    拆分：模型给出 {LAST_SPLIT.get('given', 0)} 条，保留 {len(claims)} 条"
            + (f"；丢弃 {len(dropped)} 条：{'、'.join(dropped[:6])}" if dropped else ""))
    name = candidate_name or profile.name or (claims[0].entities.get("name", "") if claims else "")
    from .faculty_recall import enrich_input
    claims = enrich_input(claims,doc.safe_text,name)
    claims = with_candidate_identity_context(claims, name)
    skip = skip_categories()
    skipped = [c for c in claims if c.category in skip]
    if skipped:
        claims = [c for c in claims if c.category not in skip]
        kinds = "、".join(dict.fromkeys(c.category for c in skipped))
        say(f"    {kinds} {len(skipped)} 条按设置不核验（SKIP_CATEGORIES）")
    return PreparedResume(resume_path=str(resume_path), doc=doc, llm=llm, profile=profile,
                          claims=claims, name=name, skipped=skipped)


def verify_prepared(
    prepared: PreparedResume,
    *,
    claim_ids=None,
    position: str = "",
    report_id: str = "live",
    searcher: Searcher | None = None,
    budget_factory=None,
    fetcher: PageFetcher | None = None,
    progress=None,
) -> Report:
    """第 3–6 步：只核验勾选的陈述（claim_ids 为 None 时核验全部），报告按简历原顺序排列。"""
    doc, llm, profile, name = prepared.doc, prepared.llm, prepared.profile, prepared.name
    resume_path = prepared.resume_path
    say = progress or (lambda *a, **k: None)
    searcher = CachedSearcher(searcher or build_searcher())
    from .deepseek_fetch import build_fetcher
    fetcher = fetcher or build_fetcher()
    # 调用方显式给了预算工厂，就完全照它走（验收里就是用它把预算压到 1 次来跑断路场景）；
    # 没给的话才由**检索计划自己带预算**——档案说 8 次检索、执行层却仍用默认 6 的话，
    # 多出来的查询永远不会执行，差异化策略就成了装饰。
    explicit_budget = budget_factory is not None
    budget_factory = budget_factory or Budget
    contextual_claims = with_candidate_identity_context([*prepared.claims, *prepared.skipped], name)
    from .cost_budget import CURRENT,ClaimShare,ClaimAllocations,bind as bind_cost
    from .retrieval_policy import enabled as recall_enabled
    money=CURRENT.get()
    fair=bool(money and getattr(money,'enforced',True) and recall_enabled() and not explicit_budget)
    # Website discovery must not consume the funds intended for all claim phases.
    setup=ClaimShare(money,min(.1,max(0,money.limit-money.spent)*.05),search_fraction=1) if fair else money
    with bind_cost(setup):sources = employer_policy(contextual_claims, doc, name, searcher, say)
    current_orgs = sources.get('organizations', []) if isinstance(sources, dict) else []
    if current_orgs:
        # 全文明示的任职可能被拆分遗漏；仍用于第三方来源的同名身份核对。
        for c in contextual_claims:
            context = c.entities['candidate_identity_context']
            context['current_employers'] = list(dict.fromkeys([*context['current_employers'], *sources.get('current_employers', [])]))
    skipped_ids = {claim.id for claim in prepared.skipped}
    contextual_claims = [claim for claim in contextual_claims if claim.id not in skipped_ids]
    if claim_ids is None:
        claims = contextual_claims
    else:
        wanted = set(claim_ids)
        claims = [c for c in contextual_claims if c.id in wanted]
        if not claims:
            raise ValueError("没有选择要核验的条目")
        if len(claims) < len(prepared.claims):
            say(f"    只核验勾选的 {len(claims)} 条（共 {len(prepared.claims)} 条）")

    # 同一份简历里常有多条来自同一所学校的陈述。未知学校的官网一旦确认，后续陈述
    # 直接复用，避免重复跑“学校名 + 官网”发现查询，既省时间也省搜索额度。
    # （并行时几条陈述可能同时去找同一所学校的官网，找到后照样写进这里共用。）
    school_domains: dict[str, str] = {}
    organizer_domains: dict[str, str] = {}
    publisher_domains: dict[str, str] = {}
    # 档案匹配要用整份简历的类别集合，不能只看当前这一条——见 strategy._matches
    # （跳过不核验的条目也算：它们照样说明这是一份什么样的简历）
    resume_categories = [c.category for c in [*prepared.claims, *prepared.skipped]]
    source_urls = (getattr(doc, "source_urls", None) or [doc.source]) if getattr(doc, "kind", "") == "web" else []
    if source_urls:
        say("输入主页及其跳转地址只用于拆分陈述，不作为本次检验的证据")
    if current_orgs:
        say('从第三方核对经历，排除当前所属机构渠道：' + '、'.join(current_orgs) + '；仅本校就读/本校荣誉按条目允许本校材料')
    total = len(claims)
    allocations=ClaimAllocations(money,[c.id for c in claims]) if fair else None
    workers = claim_workers(total)
    done = 0
    done_lock = threading.Lock()
    coordinator = VerificationCoordinator()
    improved = ict_pipeline.enabled()
    parallel_agents = ict_pipeline.parallel_enabled()
    from .source_repairs import enabled as repair_sources

    def run_one(i: int, claim):
        scope = for_claim(claim, sources)
        claim_searcher = SourceGuardSearcher(searcher, scope) if scope else searcher
        claim_fetcher = SourceGuardFetcher(fetcher, scope) if scope else fetcher
        claim = claim.model_copy(update={"entities": {**claim.entities, "candidate_name": name,
                                                     "excluded_source_urls": source_urls,
                                                     "excluded_employer_sources": scope,
                                                     "identity_school_domains": school_domains}})
        started = time.monotonic()
        specialist = agent_for(claim)
        # 并行时各条的日志交错输出，每行带上陈述编号才分得清
        def csay(*a, **k):
            msg = " ".join(str(x) for x in a).strip()
            say(f"    [{claim.id}] {msg}" if workers > 1 else f"    {msg}")

        # 3 检索计划（策略档案驱动）
        plan = plan_with_notes(claim, name, profile, resume_categories)
        csay(specialist.name + "：按本条目的核对，已核实线索仅用于定位")
        if workers > 1:
            csay(f"开始：{claim.category} · {plan.profile_id} · {len(plan.queries)} 条查询"
                 f" / 预算 {'不限' if unlimited() else plan.budget.get('searches')} 次")
        else:
            say(f"[3-5/6] ({i}/{total}) {claim.id} {claim.category}"
                f" · {plan.profile_id} · {len(plan.queries)} 条查询 / 预算 {'不限' if unlimited() else plan.budget.get('searches')} 次")
        # 4 检索与页面读取
        if explicit_budget:
            budget = budget_factory()
        else:
            seconds = plan.budget.get("seconds", MAX_SECONDS)
            if workers > 1:
                seconds = int(seconds * PARALLEL_TIME_FACTOR)
            budget = budget_factory(
                max_searches=plan.budget.get("searches", MAX_SEARCHES),
                max_page_reads=plan.budget.get("reads", MAX_PAGE_READS),
                max_seconds=seconds,
            )
        if improved:
            budget.max_seconds = min(budget.max_seconds, read_remaining())
        if claim.category == "实习":
            task = coordinator.start_task(claim, plan, budget)
            vc = specialist.collect(task, claim, name, claim_searcher, llm,
                                    collector=collect_for_claim, manual_collector=build_manual_review,
                                    budget=budget, fetcher=claim_fetcher, progress=csay)
            vc.verification_agent = task.finish(vc)
            if workers > 1:
                nonlocal done
                with done_lock:
                    done += 1
                    k = done
                say(f"    ✓ ({k}/{total}) {claim.id} 实习 完成：人工背调入口（{time.monotonic() - started:.0f} 秒）")
            return vc
        task = coordinator.start_task(claim, plan, budget)
        evidences, exhausted = specialist.collect(
            task,
            claim, name, claim_searcher, llm,
            collector=collect_for_claim, manual_collector=build_manual_review,
            budget=budget, fetcher=claim_fetcher, queries=plan.queries,
            progress=csay if workers > 1 else say,
            school_domains=school_domains,
            organizer_domains=organizer_domains,
            publisher_domains=publisher_domains,
        )
        if scope:
            dropped = [e for e in evidences if evidence_excluded(e, scope)]
            if dropped:
                kept = [e for e in evidences if not evidence_excluded(e, scope)]
                # 保留专用审计列表元数据，不能在过滤时变回普通 list。
                evidences[:] = kept
                csay("排除输入主页或现任机构渠道的材料，只保留独立第三方证据")
        # 5 判定：全部规则
        vc = judge.assess(claim, evidences, search_exhausted=exhausted)
        from .cost_budget import CURRENT
        money=CURRENT.get()
        paused=money and (money.stopped or getattr(money,'search_stopped',False))
        if paused:
            vc.source_notes.append('费用预算限制：'+money.reason+' 本轮不完整检索不能用于认定经历不实。')
        if not parallel_agents:
            vc.verification_agent = task.finish(vc)
        if not vc.github_audit:
            vc.source_notes.append("只有姓名或发布时间一致的记录不纳入证据；学校、学段、任职及具体经历须能与本次输入关联。")
        if claim.category == "学历" and study_stage(claim):
            vc.source_notes.append(f"本条核对{study_stage(claim)}阶段；同校其他学段不能替代，推荐资格、录取、就读和学位授予分别核对。")
        elif claim.category in AWARD_CATEGORIES:
            vc.source_notes.append("本条奖项单独取证：就读、推免、录取或毕业不能证明本人获得该奖。")
        competition = competition_for_claim(claim)
        resolution_note = competition_resolution_note(claim)
        if resolution_note:
            vc.source_notes = [resolution_note]
        if competition:
            vc.competition_lookup = lookup_summary(competition, claim)
            vc.source_notes = [publication_note(competition, claim)]
            failures = getattr(fetcher, "failures", {})
            entry_failures = list(dict.fromkeys(failures[p["url"]] for p in seed_pages(competition, claim)
                                                if failures.get(p["url"])))
            if entry_failures:
                vc.source_notes.append("本次部分官网名单入口未能回读（" + "、".join(entry_failures)
                                       + "）；访问失败不等于官网不提供名单，也不代表本人未获奖。")
            if not evidences and re.search(r"[A-Za-z]", name) and not re.search(r"[\u3400-\u9fff]", name):
                vc.source_notes.append("当前只提供英文姓名；中文名单中的同名拼音不能自动认定为本人，可补充中文姓名或参赛队号建立关联。")
        vc.next_step = default_next_step(vc)
        if not plan.queries and not evidences:
            vc.needs_human = True
            vc.next_step = '尚未执行有效查询，请补充论文题名、DOI或具体赛事/机构等检索信息后再核验。'
            vc.source_notes.append('未执行检索：本条没有生成可用查询，不能解释为已经搜索后未找到。')
        elif paused and budget.exhausted:
            vc.needs_human=True
            vc.next_step=('本条只完成部分检索，已为其他事项保留费用；已有证据保留，请补充可追溯材料核对未覆盖部分。'
                          if budget.searches or budget.page_reads else '本条因费用不足未执行检索；不能作为查无记录的结论，请补充可追溯材料。')
        elif task.snapshot()['execution']['state']=='time_limited':
            vc.needs_human=True
            vc.next_step='本条达到执行时限，部分查询或原文未能读取。已有证据保留，请复核未覆盖要素。'
        elif repair_sources() and not evidences:
            audit = task.snapshot()
            reasons = [reason.partition("：")[2] for reason in audit["decisionCounts"]
                       if reason.startswith("本人关联不足：")]
            if reasons and audit["counts"].get("read", {}).get("returned"):
                detail = next((reason for reason in reasons if "只有姓名" in reason), reasons[0])
                vc.needs_human = True
                vc.next_step = f"找到并读取了公开资料，身份关联不足：{detail}。请补充可核对的本人记录。"
        vc.source_notes.extend(n for n in plan.notes if '缺字段' in n or '未提供论文题名' in n)
        if current_orgs:
            allowed = scope.get('allowed_academic_institutions', []) if isinstance(scope, dict) else []
            if allowed:
                vc.source_notes.append('本条允许对应学校（' + '、'.join(allowed) + '）证明本人在本校就读/获本校荣誉；仍核对学段、年份、奖项及本人身份，输入主页不能自证，不能借此证明其他经历。')
            else:
                vc.source_notes.append('本条从第三方取证，已排除现任机构/当前就读学校（' + '、'.join(current_orgs) + '）的官网、子站及可识别的官方发布主体；这些机构的材料不用于证明本条陈述。')
        if any(e.extraction_method == "ocr" for e in evidences):
            vc.source_notes.append("已读取官方名单图片的OCR文本；姓名、奖级及合并单元格可能识别错位，需人工核对原图，不能仅据OCR升级为已证实。")
        if any(e.extraction_method == "search_snippet" for e in evidences):
            vc.source_notes.append("部分原文访问失败，仅保留搜索引擎收录的本人摘要作为线索；未回读原文的摘要不能单独完成核验。")
        # 6 澄清问题
        if not (vc.github_audit or vc.claim_verification or paused or not plan.queries and not evidences):
            if not parallel_agents:
                vc.question = generate_question(vc, llm)
            elif read_remaining() >= 5:
                try:
                    vc.question = generate_question(vc, llm)
                except Exception as exc:
                    csay(f"澄清问题生成失败（{type(exc).__name__}），已保留核验结果")
        if vc.github_audit:
            vc.next_step = "只补充本条项目的本人账号、贡献链接和角色/指标的可追溯材料；未公开部分保持未知。"
        if parallel_agents:
            vc.verification_agent = task.finish(vc)
        if workers > 1:
            with done_lock:
                done += 1
                k = done
            say(f"    ✓ ({k}/{total}) {claim.id} {claim.category} 完成："
                f"{STATUS_LABEL.get(vc.status, vc.status)}（{time.monotonic() - started:.0f} 秒）")
        return vc

    def verify_one(i,claim):
        with allocations.claim(claim.id) if allocations else nullcontext():
            if not parallel_agents:
                return run_one(i, claim)
            deadline_token = None
            if improved:
                end = time.monotonic() + min(ict_pipeline.CLAIM_SECONDS, read_remaining())
                deadline_token = READ_DEADLINE.set(end)
            evidence = []
            upstream_observer = EVIDENCE_OBSERVER.get()

            def retain(evaluated_claim, item):
                evidence.append((evaluated_claim, item))
                if upstream_observer:
                    upstream_observer(evaluated_claim, item)

            observer_token = EVIDENCE_OBSERVER.set(retain)
            try:
                return run_one(i, claim)
            except Exception as exc:
                from .coverage import from_claim
                evaluated_claim = evidence[-1][0] if evidence else claim
                scope = for_claim(evaluated_claim, sources)
                kept = [item for _, item in evidence if not evidence_excluded(item, scope)]
                vc = judge.assess(evaluated_claim, kept, search_exhausted=True)
                vc.needs_human = True
                vc.next_step = f"本条核验发生程序异常（{type(exc).__name__}），可单独复核。"
                vc.source_notes.append(vc.next_step)
                say(f"    [{claim.id}] {vc.next_step}")
                task = coordinator.task_for(claim.id)
                if task is None:
                    vc.retrieval_status = "provider_error"
                    vc.retrieval_summary = from_claim(vc, retrieval_status=vc.retrieval_status,
                        limitations=[{"code": type(exc).__name__, "message": "核验计划未能建立"}])
                else:
                    task.fail(exc)
                    vc.verification_agent = task.finish(vc)
                return vc
            finally:
                EVIDENCE_OBSERVER.reset(observer_token)
                if deadline_token is not None:
                    READ_DEADLINE.reset(deadline_token)

    # 先核对本科，再核对硕士/博士就读及入学路径，最后独立核对奖项等条目。
    # 每个阶段内部仍并行，报告维持输入原顺序；本科无公开证据不会阻止独立核对研究生。
    by_id = {}
    def receive(result):
        by_id[result.claim.id] = result
        coordinator.record([result])

    if parallel_agents:
        say(f"[3-5/6] 类别 Agent 协作核验，同步处理最多 {workers} 条")
        def dispatched(claim):
            coordinator.record_dispatch(claim)
            return coordinator.context_for(with_verified_education_context(claim, list(by_id.values())))

        ict_pipeline.run_agents(claims, workers, verify_one, receive, dispatched)
    else:
        for label, ready in coordinator.schedule(claims):
            group = [(i, coordinator.context_for(with_verified_education_context(c, list(by_id.values()))))
                     for i, c in ready]
            say(f"[3-5/6] {label}：{len(group)} 条")
            if workers > 1 and len(group) > 1:
                say(f"    {len(group)} 条陈述" + ("同时开始检索" if workers >= len(group) else f"并行检索（同时 {workers} 条）"))
                with ThreadPoolExecutor(max_workers=min(workers, len(group))) as pool:
                    results = [f.result() for f in [pool.submit(verify_one, i, c) for i, c in group]]
            else:
                results = [verify_one(i, c) for i, c in group]
            for result in results:
                receive(result)
    verified = [by_id[c.id] for c in claims]

    # 只在学历条目之间复用对应学段的直接就读/推荐记录，不能跨学位或从奖项借证。
    changed = judge.corroborate_enrollment(verified, name, school_domains)
    for idx in changed:
        vc = verified[idx]
        vc.next_step = default_next_step(vc)
        vc.question = generate_question(vc, llm)
        say(f"    关联佐证：{vc.claim.id} 学历 借用同份简历其他条目的官方名单，"
            f"状态更新为 {vc.status}")

    # Institutional navigation has its own small budget; evidence exhaustion must not
    # hide the fallback. Explicit budget factories still control this auxiliary work.
    pending = [v for v in verified if v.claim.category == "学历" and v.status != "ok"]
    if pending:
        from .education_review import build_education_review
        say("[6/6] 补充未完全核实学历的学校/专业官网与公开办公联系渠道")
        def add_education_context(vc):
            context_budget = budget_factory() if explicit_budget else Budget(
                max_searches=3, max_page_reads=4, max_seconds=45)
            try:
                vc.education_review = build_education_review(
                    vc.claim, searcher, fetcher, context_budget, school_domains, say)
            except Exception:
                vc.education_review = {"kind": "education", "school": "", "field": "",
                    "links": [], "contacts": [], "profile": {},
                    "note": "机构资料仅作人工核验入口，不证明本人学历。",
                    "limitations": ["本次官网辅助资料暂不可用，可请候选人提供院系官网或学籍管理部门公开联系页。"]}
        with ThreadPoolExecutor(max_workers=min(3, len(pending))) as pool:
            list(pool.map(add_education_context, pending))

    from .contact_fallback import attach_contact_reviews
    attach_contact_reviews(verified, searcher, fetcher,
        budget_factory if explicit_budget else lambda: Budget(max_searches=2, max_page_reads=4, max_seconds=30),
        school_domains, say)

    say("[6/6] 汇总报告")
    from .cost_budget import CURRENT
    money=CURRENT.get()
    if money and money.stopped:
        for item in verified:
            if not any(note.startswith('费用预算限制：') for note in item.source_notes):
                item.source_notes.append('费用预算限制：'+money.reason+' 本轮不完整检索不能用于认定经历不实。')
    from .coverage import report_coverage
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
        skipped_claims=list(prepared.skipped),
        verification_workflow=coordinator.summary(verified),
        coverage=report_coverage(verified, prepared.skipped),
    )


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
    parsed_document: ParsedDocument | None = None,
    on_profile=None,
) -> Report:
    """live 模式。judge.assess 是唯一的判定入口，与 fixture 走的是同一套规则。

    profile 是简历画像（身份/行业/层级/技能），决定用哪套检索策略。
    不给就走兜底档案——预算更低，因为不知道去哪找时广撒网只换噪音。
    = prepare_resume（解析、画像、拆分）+ verify_prepared（全部陈述）。
    """
    prepared = prepare_resume(resume_path, candidate_name=candidate_name, profile=profile, llm=llm,
                              progress=progress, parsed_document=parsed_document, on_profile=on_profile)
    return verify_prepared(prepared, position=position, report_id=report_id, searcher=searcher,
                           budget_factory=budget_factory, fetcher=fetcher, progress=progress)


def run(resume_path: str | Path | None = None, **kw) -> Report:
    """按 MODE 环境变量选择模式，默认 mock。"""
    if os.environ.get("MODE", "mock") != "live":
        return load_mock_report()
    if resume_path is None:
        raise SystemExit("live 模式需要提供简历路径")
    return run_pipeline(resume_path, **kw)
