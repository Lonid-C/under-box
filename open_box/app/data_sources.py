"""Executable source contracts and bounded, report-local access records.

Catalogs locate sources; only fetched, identity-checked records can be evidence.
Existing search credentials and public API adapters remain the actual transport.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import asdict
from collections import Counter
from threading import Lock
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import re


ALL_CATEGORIES = ("学历", "校内荣誉", "奖学金", "学生工作", "竞赛", "论文", "专利", "会议",
                  "项目", "开源项目", "实习", "任职", "教学活动", "艺术成果", "著作")


@dataclass(frozen=True)
class DataSource:
    id: str
    name: str
    categories: tuple[str, ...]
    adapter: str
    role: str = "public_record"
    authorization: str = "public_only"

    def describe(self):
        return {"id": self.id, "name": self.name, "adapter": self.adapter, "role": self.role,
                "authorization": self.authorization, "automaticPrivateAccess": False}


SOURCES = {s.id: s for s in (
    DataSource("school_catalog", "学校域名与公示栏目目录", ALL_CATEGORIES, "plan.school_domain / school_archives", "navigation_only"),
    DataSource("competition_catalog", "赛事及历年名单入口目录", ("竞赛",), "competitions.competition_for_claim / seed_pages", "navigation_only"),
    DataSource("school_records", "学校公开培养、招生及荣誉记录", ("学历", "校内荣誉", "奖学金", "竞赛", "学生工作"), "searcher + PageFetcher / public attachments"),
    DataSource("public_web", "独立第三方公开网页", tuple(c for c in ALL_CATEGORIES if c != "实习"), "account-bound searcher + PageFetcher"),
    DataSource("paper_metadata", "Crossref / OpenAlex 论文登记记录", ("论文",), "search.paper_hits", "public_metadata"),
    DataSource("publisher_records", "期刊与出版社原始记录", ("论文", "著作"), "searcher + PageFetcher"),
    DataSource("book_catalogs", "图书馆与出版社公开书目", ("著作",), "searcher + PageFetcher / book_catalog"),
    DataSource("patent_records", "专利公开登记与公告", ("专利",), "searcher + PageFetcher (patent route)"),
    DataSource("conference_records", "会议主办方议程与报告记录", ("会议",), "searcher + PageFetcher"),
    DataSource("competition_records", "赛事主办方正式赛果与名单", ("竞赛",), "searcher + PageFetcher / roster attachments"),
    DataSource("github_records", "本项目 GitHub 公开贡献记录", ("项目", "开源项目"), "github_audit.audit_claim / GitHubClient", "provided_project_only"),
    DataSource("qcc_contacts", "企查查公司公开联系方式", ("实习",), "internship.build_manual_review", "manual_review_only"),
)}


class SourceNotAllowed(ValueError):
    pass


_ACTIVE_RUN = ContextVar("underbox_agent_source_run", default=None)


def safe_reference(value):
    """No credentials, secret URL parameters or raw provider exceptions in logs."""
    value = str(value or "")
    if value.startswith(("https://", "http://")):
        try:
            u = urlsplit(value)
            query = [(k, "[redacted]" if re.search(r"key|token|password|secret|authorization", k, re.I) else v)
                     for k, v in parse_qsl(u.query, keep_blank_values=True)]
            value = urlunsplit((u.scheme, u.netloc.rsplit("@", 1)[-1], u.path, urlencode(query), ""))
        except ValueError:
            return "[invalid URL]"
    value = re.sub(r"(?i)(api[_-]?key|token|password|secret)\s*[=:]\s*[^\s&]+", r"\1=[redacted]", value)
    return value[:500]


class AgentRun:
    """One task, one budget. Parallel child tools inherit this ContextVar."""

    def __init__(self, agent, claim, plan, budget):
        self.agent, self.claim, self.plan, self.budget = agent, claim, plan, budget
        self.events = []
        self.truncated = False
        self._lock = Lock()
        self.status = "running"
        self._elapsed = None
        self.counts = Counter()

    @contextmanager
    def bind(self):
        token = _ACTIVE_RUN.set(self)
        try:
            yield self
        finally:
            _ACTIVE_RUN.reset(token)

    def add(self, event):
        with self._lock:
            self.counts[(event.get("action", ""), event.get("state", ""))] += 1
            if len(self.events) < 80:
                self.events.append(event)
            else:
                self.truncated = True

    def finish(self, verified):
        self.status = verified.status
        if self._elapsed is None:
            self._elapsed = self.budget.elapsed()
        from .coverage import from_claim
        stats = self.snapshot()
        counts = stats["counts"]
        searches = counts.get("search", {})
        reads = counts.get("read", {})
        successful = sum(searches.get(s, 0) + reads.get(s, 0) for s in ("returned", "empty"))
        failed = sum(searches.get(s, 0) + reads.get(s, 0) for s in ("failed", "blocked"))
        attempted = successful + failed
        manual = verified.claim.category == "实习"
        status = ("partial" if successful else "provider_error") if failed else (
            "budget_exhausted" if self.budget.exhausted else "complete" if attempted else "not_attempted")
        if manual:
            status = "not_attempted"  # Company contact lookup is not internship fact retrieval.
        verified.retrieval_status = status
        verified.retrieval_summary = from_claim(verified, retrieval_status=status,
            queries_planned=len(self.plan.queries),
            queries_attempted=sum(searches.get(s, 0) for s in ("returned", "empty", "failed")),
            query_successes=sum(searches.get(s, 0) for s in ("returned", "empty")),
            pages_attempted=sum(reads.get(s, 0) for s in ("returned", "empty", "failed")), pages_read_success=reads.get("returned", 0),
            page_failures=[e for e in stats["events"] if e.get("action") == "read" and e.get("state") in {"failed", "blocked"}],
            unverified_lead_count=sum(e.provenance_state not in {"page_text_verified", "structured_metadata_verified"} for e in verified.evidence),
            limitations=[{"code": e.get("error", "source_unavailable"), "message": e.get("reason", "部分来源未能完成访问")}
                         for e in stats["events"] if e.get("state") in {"failed", "blocked"}])
        return {**self.agent.describe(), "reusedAnchors": self.claim.entities.get("verified_identity_anchors", []),
                 "sourceAccess": stats, "handoffs": self.plan.handoffs,
                 "runtimePlan": {"strategy": {"id": self.plan.profile_id, "name": self.plan.profile_name},
                    "budget": dict(self.plan.budget), "queries": [asdict(q) for q in self.plan.queries],
                    "droppedByBudget": list(self.plan.dropped), "notes": list(self.plan.notes)}}

    def snapshot(self):
        with self._lock:
            events = [dict(e) for e in self.events]
            counts = {}
            for (action, state), n in self.counts.items():
                counts.setdefault(action, {})[state] = n
        return {"claimId": self.claim.id, "agentId": self.agent.id, "status": self.status,
                 "events": events, "truncated": self.truncated, "counts": counts,
                "budget": {"maxSearches": self.budget.max_searches, "searches": self.budget.searches,
                           "maxReads": self.budget.max_page_reads, "reads": self.budget.page_reads,
                           "seconds": round(self._elapsed if self._elapsed is not None else self.budget.elapsed(), 2), "maxSeconds": self.budget.max_seconds},
                "note": "数据源调用记录只说明查询过程，不构成本人经历证据。"}


def invoke_source(source_id, operation, *, reference="", action="search", category=None, unavailable_reason=None):
    source = SOURCES.get(source_id)
    run = _ACTIVE_RUN.get()
    category = category or (run.claim.category if run else None)
    if source is None or (category and category not in source.categories):
        if run:
            run.add({"sourceId": source_id, "sourceName": source.name if source else "未知数据源",
                     "action": action, "state": "blocked", "attempted": False,
                     "error": "SourceNotAllowed", "reason": "该类别未授权使用此数据源"})
        raise SourceNotAllowed("该类别未授权使用此数据源")
    if run and source_id not in run.agent.source_ids:
        run.add({"sourceId": source_id, "sourceName": source.name, "action": action,
                 "state": "blocked", "attempted": False, "error": "SourceNotAllowed",
                 "reason": "当前专项核验未授权使用此数据源"})
        raise SourceNotAllowed("当前专项核验未授权使用此数据源")
    started = time.monotonic()
    event = {"sourceId": source_id, "sourceName": source.name, "action": action,
             "reference": safe_reference(reference), "attempted": True}
    try:
        result = operation()
        event["state"] = "returned" if result else "empty"
        reason = unavailable_reason() if not result and callable(unavailable_reason) else ""
        if reason:
            event.update(state="failed", error="ReadUnavailable", reason=safe_reference(reason))
        if isinstance(result, list):
            event["records"] = len(result)
        return result
    except Exception as exc:
        event.update(state="failed", error=type(exc).__name__)
        raise
    finally:
        if run:
            event["seconds"] = round(time.monotonic() - started, 3)
            run.add(event)


def source_for_query(claim, query=None, site=None):
    kind = getattr(query, "kind", "web")
    source = getattr(query, "source", "")
    if claim.category == "实习":
        return "qcc_contacts"
    if kind == "crossref":
        return "paper_metadata"
    if kind == "github":
        return "github_records"
    if kind == "patent" or claim.category == "专利":
        return "patent_records"
    if claim.category == "著作":
        return "book_catalogs"
    if claim.category == "会议":
        return "conference_records"
    if claim.category == "竞赛" and (getattr(query, "site", "") == "organizer" or source.startswith("organizer:")):
        return "competition_records"
    if claim.category == "论文" and (getattr(query, "site", "") == "publisher" or source.startswith("publisher:")):
        return "publisher_records"
    if (claim.category in SOURCES["school_records"].categories
            and (getattr(query, "site", "") in {"school", "source_school"}
                 or source.startswith(("school:", "source_school:", "education_route:")))):
        return "school_records"
    return "public_web"


class SourceSearcher:
    def __init__(self, inner, run):
        self.inner, self.run = inner, run

    def _call(self, method, query, site=None, **kwargs):
        planned = next((q for q in self.run.plan.queries if q.text == query or query.startswith(q.text + " -site:")), None)
        source = source_for_query(self.run.claim, planned, site)
        return invoke_source(source, lambda: method(query, site=site, **kwargs), reference=query,
                             action="search", category=self.run.claim.category)

    def search(self, query, site=None):
        return self._call(self.inner.search, query, site)

    def __getattr__(self, name):
        if name not in {"confirm_empty", "search_with_confirmation"}:
            raise AttributeError(name)
        value = getattr(self.inner, name, None)
        if callable(value):
            return lambda query, site=None, **kw: self._call(value, query, site, **kw)
        raise AttributeError(name)


class SourceFetcher:
    def __init__(self, inner, run):
        self.inner, self.run = inner, run

    def _read(self, url, method, *args, **kwargs):
        source = source_for_query(self.run.claim)
        if self.run.claim.category in {"学历", "校内荣誉", "奖学金", "学生工作"}:
            source = "school_records"
        def unavailable():
            failures = getattr(self.inner, "failures", None)
            return failures.get(url, "") if isinstance(failures, dict) else ""
        return invoke_source(source, lambda: method(url, *args, **kwargs), reference=url,
                             action="read", category=self.run.claim.category,
                             unavailable_reason=unavailable)

    def get(self, url, *args, **kwargs):
        return self._read(url, self.inner.get, *args, **kwargs)

    def get_roster(self, url, claim):
        method = getattr(self.inner, "get_roster", None)
        return self._read(url, method, claim) if callable(method) else self.get(url)

    def __getattr__(self, name):
        if name not in {"failures", "resolved_urls", "post_public_json", "read_bnbu_profile_sections"}:
            raise AttributeError(name)
        value = getattr(self.inner, name)
        if callable(value):
            return lambda url, *a, **kw: self._read(url, value, *a, **kw)
        return value
