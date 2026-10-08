"""六步流水线编排（第 5 节）。

简历/个人主页
  → 1 解析与隐藏文字检测
  → 2 陈述拆分（LLM）
  → 3 检索计划（规则）
  → 4 检索与页面读取（受预算约束）
  → 5 证据提取（LLM）+ 身份判定 + 来源分级 + 状态判定（全部规则）
  → 6 澄清问题生成（LLM）
  → 报告 JSON

并行（2026-10-07）：画像与陈述拆分同时调用模型；拆出来的每一条陈述**同时开始**
检索、回读、判定和生成澄清问题（`CLAIM_WORKERS`，默认 0 = 全部同时开始；设 1 恢复
逐条串行）。报告里的陈述顺序不变。模型调用另有总并发上限（`LLM_CONCURRENCY`，默认 10），
搜索 API 由 search.py 统一错峰，学校官网仍按单域串行、间隔 1 秒，不会因为并行而加压。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import judge
from .collect import Budget, collect_for_claim
from .llm import LLM, build_llm
from .competitions import (competition_for_claim, competition_resolution_note, lookup_summary,
                           publication_note, seed_pages)
from .parse import ParsedDocument, parse_document
from .plan import MAX_PAGE_READS, MAX_SEARCHES, MAX_SECONDS, plan_with_notes
from .profile import derive_profile
from .resume_gate import ensure_resume
from .questions import default_next_step, generate_question
from .homepage import same_page
from .schema import STATUS_LABEL, Report
from .search import CachedSearcher, PageFetcher, Searcher, build_searcher
from .split import split_claims
from .strategy import add_admission_source_hints, add_competition_school_hints

ROOT = Path(__file__).resolve().parent.parent


def load_mock_report(report_id: str = "lin") -> Report:
    """mock 模式：前端与 live 模式完全一致，只是流水线换成读取 fixture JSON。"""
    p = ROOT / "fixtures" / f"report_{report_id}.json"
    return Report(**json.loads(p.read_text(encoding="utf-8")))


def claim_workers(n: int) -> int:
    """同时处理几条陈述。CLAIM_WORKERS=0/未设 → 全部同时开始；1 → 逐条串行（旧行为）。"""
    try:
        w = int(os.environ.get("CLAIM_WORKERS", "0") or 0)
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
    """拆出来但不进入核验的类别。默认跳过「实习」：企业实习几乎没有公开记录，
    检索只会烧预算、给出一排「未找到」。SKIP_CATEGORIES 可改（逗号分隔；设为 - 表示都核验）。"""
    raw = os.environ.get("SKIP_CATEGORIES")
    if raw is None:
        raw = "实习"
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
    skipped: list = None          # 按 SKIP_CATEGORIES 跳过、不核验的陈述（默认实习）

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
        return add_competition_school_hints(add_admission_source_hints(split_claims(doc.safe_text, llm)))

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
    fetcher = fetcher or PageFetcher()
    # 调用方显式给了预算工厂，就完全照它走（验收里就是用它把预算压到 1 次来跑断路场景）；
    # 没给的话才由**检索计划自己带预算**——档案说 8 次检索、执行层却仍用默认 6 的话，
    # 多出来的查询永远不会执行，差异化策略就成了装饰。
    explicit_budget = budget_factory is not None
    budget_factory = budget_factory or Budget
    if claim_ids is None:
        claims = list(prepared.claims)
    else:
        wanted = set(claim_ids)
        claims = [c for c in prepared.claims if c.id in wanted]
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
    source_page = doc.source if getattr(doc, "kind", "") == "web" else ""
    total = len(claims)
    workers = claim_workers(total)
    done = 0
    done_lock = threading.Lock()

    def verify_one(i: int, claim):
        started = time.monotonic()
        # 并行时各条的日志交错输出，每行带上陈述编号才分得清
        def csay(*a, **k):
            msg = " ".join(str(x) for x in a).strip()
            say(f"    [{claim.id}] {msg}" if workers > 1 else f"    {msg}")

        # 3 检索计划（策略档案驱动）
        plan = plan_with_notes(claim, name, profile, resume_categories)
        if workers > 1:
            csay(f"开始：{claim.category} · {plan.profile_id} · {len(plan.queries)} 条查询"
                 f" / 预算 {plan.budget.get('searches')} 次")
        else:
            say(f"[3-5/6] ({i}/{total}) {claim.id} {claim.category}"
                f" · {plan.profile_id} · {len(plan.queries)} 条查询 / 预算 {plan.budget.get('searches')} 次")
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
        evidences, exhausted = collect_for_claim(
            claim, name, searcher, llm,
            budget=budget, fetcher=fetcher, queries=plan.queries,
            progress=csay if workers > 1 else say,
            school_domains=school_domains,
            organizer_domains=organizer_domains,
            publisher_domains=publisher_domains,
        )
        if source_page:
            # 网页输入：陈述就是从这个主页拆出来的，再拿它当证据等于自己证明自己
            dropped = [e for e in evidences if same_page(e.url, source_page)]
            if dropped:
                evidences = [e for e in evidences if not same_page(e.url, source_page)]
                csay("检索结果里有提交的主页本身，不作为证据（自述不能自证）")
        # 5 判定：全部规则
        vc = judge.assess(claim, evidences, search_exhausted=exhausted)
        competition = competition_for_claim(claim)
        resolution_note = competition_resolution_note(claim)
        if resolution_note:
            vc.source_notes = [resolution_note]
        if competition:
            vc.competition_lookup = lookup_summary(competition)
            vc.source_notes = [publication_note(competition)]
            failures = getattr(fetcher, "failures", {})
            entry_failures = list(dict.fromkeys(failures[p["url"]] for p in seed_pages(competition, claim)
                                                if failures.get(p["url"])))
            if entry_failures:
                vc.source_notes.append("本次部分官网名单入口未能回读（" + "、".join(entry_failures)
                                       + "）；访问失败不等于官网不提供名单，也不代表本人未获奖。")
            if not evidences and re.search(r"[A-Za-z]", name) and not re.search(r"[\u3400-\u9fff]", name):
                vc.source_notes.append("当前只提供英文姓名；中文名单中的同名拼音不能自动认定为本人，可补充中文姓名或参赛队号建立关联。")
        vc.next_step = default_next_step(vc)
        if any(e.extraction_method == "ocr" for e in evidences):
            vc.source_notes.append("已读取官方名单图片的OCR文本；姓名、奖级及合并单元格可能识别错位，需人工核对原图，不能仅据OCR升级为已证实。")
        if any(e.extraction_method == "search_snippet" for e in evidences):
            vc.source_notes.append("部分原文访问失败，仅保留搜索引擎收录的本人摘要作为线索；未回读原文的摘要不能单独完成核验。")
        # 6 澄清问题
        vc.question = generate_question(vc, llm)
        if workers > 1:
            nonlocal done
            with done_lock:
                done += 1
                k = done
            say(f"    ✓ ({k}/{total}) {claim.id} {claim.category} 完成："
                f"{STATUS_LABEL.get(vc.status, vc.status)}（{time.monotonic() - started:.0f} 秒）")
        return vc

    if workers > 1 and total > 1:
        say(f"[3-5/6] {total} 条陈述"
            + ("同时开始检索" if workers >= total else f"并行检索（同时 {workers} 条）"))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(verify_one, i, c) for i, c in enumerate(claims, start=1)]
            verified = [f.result() for f in futures]       # 报告里仍按原顺序排列
    else:
        workers = 1
        verified = [verify_one(i, c) for i, c in enumerate(claims, start=1)]

    # 5' 学历的关联佐证：别的条目已经读到的本校官方名单（推免资格、奖学金、竞赛获奖）
    # 同样是"在该校就读"的公开记录。纯规则、不额外检索，只补「学校」这一个要素。
    changed = judge.corroborate_enrollment(verified, name, school_domains)
    for idx in changed:
        vc = verified[idx]
        vc.next_step = default_next_step(vc)
        vc.question = generate_question(vc, llm)
        say(f"    关联佐证：{vc.claim.id} 学历 借用同份简历其他条目的官方名单，"
            f"状态更新为 {vc.status}")

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
        skipped_claims=list(prepared.skipped),
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
