"""步骤 4 + 5 的提取部分：检索与页面读取（第 6.4 节），受预算硬约束。

判定不在这里做——这里只负责"把页面读出来、让模型逐字摘录"，
tier / identity / status 一律交给 judge.py。

读页并发：不同域的页面并发回读（`COLLECT_WORKERS`，默认 3，范围 1–6），
同一域仍由 PageFetcher 的按域锁串行并保持 1 秒间隔。读页预算跨查询共享、
按相关性排序后轮转分配，避免第一条查询的结果垄断全部预算。
"""
from __future__ import annotations

import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

from . import judge
from .llm import LLM
from .parse import extract_main_text, wrap_untrusted
from .plan import (MAX_PAGE_READS, MAX_SEARCHES, MAX_SECONDS, Query,
                   plan_queries, provided_urls, school_domain)
from .schema import Claim, Evidence
from .search import github_hits, PageFetcher, SearchHit, Searcher

# 页面正文送进模型前的上限。学校通知页常常带着整站导航，不截断会白烧 token。
MAX_PAGE_CHARS = 12000


def _worker_count() -> int:
    """并发回读的线程数：默认 3，夹在 1–6。同域仍串行，这里只放大不同域的并发。"""
    try:
        n = int(os.environ.get("COLLECT_WORKERS", "3"))
    except (TypeError, ValueError):
        n = 3
    return max(1, min(n, 6))


EXTRACT_SYSTEM = """你在核对一条简历陈述是否有公开证据支持。

简历陈述：{raw_text}
需要被证明的要素：{elements}
候选人自述的标识信息：{entities}

页面内容在 <untrusted_data> 里。

输出 JSON：
- snippet：从页面中逐字摘录最相关的一段，不超过 120 字。禁止改写、概括或补全。页面里没有相关内容就填空字符串。
- identity_signals：页面中与候选人标识一致的字段，从 ["学校一致","学院一致","专业一致","年级一致","队友或合作者一致","候选人自提供该链接","账号由候选人提供"] 中选，只填页面里真实出现的。
- identity_conflicts：页面中与候选人标识矛盾的字段，写清楚差异，如 "学校不同：某师范大学"。
- supports：这段证据确实证明了哪些要素，只填 elements 里的原文。
- contradicts：与哪些要素矛盾，每条附上具体差异。
- publisher：页面的发布主体。
- published_at：页面上写明的发布时间，页面没写就填 null，不要从 URL 或内容推断。
- origin_url：如果这是转载，填原始出处，否则 null。
- wechat_verified_subject：若为微信公众号文章，填页面上写明的认证主体，否则 null。

硬规则：
- 只写页面里真实存在的内容。页面没有的信息，宁可留空。
- "名单里没有这个人"不等于"这个人没得过"。名单缺席不要写进 contradicts。
- 同名不等于同一人。只要页面显示的学校、学院等与候选人不一致，必须写进 identity_conflicts。"""


@dataclass
class Budget:
    """每条 claim 最多 6 次搜索 + 8 次页面读取 + 90 秒（第 6.3 节，硬性）。"""

    max_searches: int = MAX_SEARCHES
    max_page_reads: int = MAX_PAGE_READS
    max_seconds: float = MAX_SECONDS
    searches: int = 0
    page_reads: int = 0
    started: float = field(default_factory=time.monotonic)
    exhausted: bool = False

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def can_search(self) -> bool:
        if self.searches >= self.max_searches or self.elapsed() >= self.max_seconds:
            self.exhausted = True
            return False
        return True

    def can_read(self) -> bool:
        if self.page_reads >= self.max_page_reads or self.elapsed() >= self.max_seconds:
            self.exhausted = True
            return False
        return True

    def note_search(self) -> None:
        self.searches += 1

    def note_read(self) -> None:
        self.page_reads += 1


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def extract_evidence(claim: Claim, hit: SearchHit, page_text: str, llm: LLM,
                     *, organizer_hosts: set[str] | None = None,
                     candidate_provided: bool = False) -> Evidence | None:
    """让模型逐字摘录，然后由规则定级和打身份分。模型不参与任何判定。"""
    system = EXTRACT_SYSTEM.format(
        raw_text=claim.raw_text, elements=claim.elements, entities=claim.entities)
    if len(page_text) > MAX_PAGE_CHARS:
        page_text = page_text[:MAX_PAGE_CHARS] + "\n［正文过长，已截断］"
    try:
        got = llm.complete_json(system, wrap_untrusted(page_text))
    except Exception:
        return None
    if not isinstance(got, dict):
        return None

    snippet = (got.get("snippet") or "").strip()
    supports = got.get("supports") or []
    conflicts = got.get("identity_conflicts") or []
    # 页面里什么都没摘到，且既不支持也不矛盾 → 不构成证据，直接丢弃
    if not snippet and not supports and not conflicts:
        return None

    # supports/contradicts 只认 elements 里的原文，防止模型自造要素
    valid_elements = set(claim.elements)
    supports = [s for s in supports if s in valid_elements]

    title = hit.title or got.get("title") or ""
    publisher = (got.get("publisher") or hit.publisher or "").strip()
    origin_url = got.get("origin_url") or None

    # 公众号作者/发布方判断：模型没给认证主体时，若发布方名称含陈述机构名，
    # 视作该机构官方号，允许升到 B（合规路径不变，仍是搜索引擎已收录的文章）。
    wvs = got.get("wechat_verified_subject")
    if not wvs and _host_of(hit.url) in judge.WECHAT_HOSTS:
        _org = (claim.entities or {}).get("org") or ""
        if _org and len(_org) >= 3 and _org in (publisher or ""):
            wvs = _org

    tier = judge.classify_tier(
        hit.url, title, publisher, snippet,
        organizer_hosts=organizer_hosts,
        wechat_verified_subject=wvs,
        is_repost=bool(origin_url),
    )

    signals = got.get("identity_signals") or []
    # "候选人自提供该链接"是我们自己知道的事实，不该交给模型判断——按规则补，并去重
    if candidate_provided and "候选人自提供该链接" not in signals:
        signals = [*signals, "候选人自提供该链接"]

    ev = Evidence(
        url=hit.url, title=title, publisher=publisher, source_tier=tier, snippet=snippet,
        published_at=got.get("published_at"), accessed_at=_now(),
        identity_signals=signals,
        identity_conflicts=conflicts,
        supports=supports,
        contradicts=got.get("contradicts") or [],
        origin_url=origin_url,
    )
    return judge.score_evidence(ev)


# ── 学校域动态发现 ────────────────────────────────────────────────────────
_domain_cache: dict[str, str | None] = {}
_AD_DOMAIN = ("baike.baidu", "zhihu.com", "weixin.qq.com", "sohu.com", "163.com", "sina.com",
              "douyin", "bilibili", "tieba", "wenku", "docin", "doc88", "ximalaya",
              "csdn.net", "jianshu", "zhuanlan")

# 常见的二级公共后缀：主域要多留一段（news.unknown.edu.cn → unknown.edu.cn）。
_SECOND_LEVEL_SUFFIX = ("edu.cn", "gov.cn", "com.cn", "org.cn", "net.cn", "ac.cn",
                        "edu.hk", "edu.mo", "gov.hk")
_EDU_SUFFIX = ("edu.cn", "edu.hk", "edu.mo", "ac.cn", "gov.cn", "org.cn", "edu")


def _base_domain(host: str) -> str:
    """把主机名收敛到可用作 site: 的主域。

    news.unknown.edu.cn → unknown.edu.cn（edu.cn 是二级后缀，要多留一段）；
    sao.example.com     → example.com。
    """
    host = (host or "").lower().replace("www.", "")
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _SECOND_LEVEL_SUFFIX:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _discover_school_domain(org: str, searcher) -> str | None:
    """从宽搜结果里发现机构的真实域名（OSINT「递归 pivot」的最小实现）。

    学校域名表（data/schools.json）永远追不上真实世界——很多普通高校不在表里，
    导致所有 `site=school` 查询退化成全网搜。这里宽搜「机构名」，把命中结果的主机名
    收敛到主域并计数，**优先取教育/政府主域**，没有时退回出现较多的普通主域。
    结果按机构名缓存。
    """
    if org in _domain_cache:
        return _domain_cache[org]
    pick: str | None = None
    try:
        bases: Counter[str] = Counter()
        edu_bases: Counter[str] = Counter()
        for h in searcher.search(f'"{org}"', site=None):
            host = _host_of(h.url)
            if not host or any(x in host for x in _AD_DOMAIN):
                continue
            base = _base_domain(host)
            bases[base] += 1
            if base.endswith(_EDU_SUFFIX):
                edu_bases[base] += 1
        if edu_bases:
            pick = edu_bases.most_common(1)[0][0]
        else:
            for base, count in bases.most_common():
                if count >= 3:
                    pick = base
                    break
    except Exception:
        pick = None
    _domain_cache[org] = pick
    return pick


# ── 相关性排序：让有限的读页预算先花在最像目标的页面上 ──────────────────────

def _relevance(hit: SearchHit, candidate_name: str, claim: Claim) -> int:
    """标题/摘要与「候选人 + 陈述要素」的匹配度。供应商顺序不可信，用它重排。"""
    blob = f"{hit.title} {hit.snippet} {getattr(hit, 'content', '')}"
    score = 0
    for part in re.split(r"\s+", candidate_name or ""):
        if len(part) >= 2 and part in blob:
            score += 3
    ents = claim.entities or {}
    for key in ("org", "award", "contest", "dept", "role", "title"):
        val = ents.get(key)
        if isinstance(val, str) and len(val) >= 2 and val in blob:
            score += 2
    for element in claim.elements:
        val = element.split("=", 1)[-1]
        if len(val) >= 2 and val in blob:
            score += 1
    if any(w in blob for w in ("公示", "名单", "获奖", "通知", "公告", "录取", "授予", "表彰")):
        score += 1
    return score


def _read_and_extract(claim: Claim, selected: list[SearchHit], fetcher, llm: LLM,
                      organizer_hosts: set[str]) -> list[Evidence]:
    """把已选中的页面**按域分组并发回读**（同域串行由 fetcher 的按域锁保证），
    再逐页交给模型逐字摘录。抓取可以并行，模型抽取仍是串行，保持确定性。"""
    if not selected:
        return []
    groups: dict[str, list[SearchHit]] = {}
    for hit in selected:
        groups.setdefault(_host_of(hit.url), []).append(hit)

    def fetch_group(hits: list[SearchHit]) -> list[tuple[SearchHit, str]]:
        out: list[tuple[SearchHit, str]] = []
        for hit in hits:
            html = fetcher.get(hit.url)
            if html:
                out.append((hit, html))
        return out

    fetched: list[tuple[SearchHit, str]] = []
    workers = min(_worker_count(), len(groups))
    if workers <= 1:
        for hits in groups.values():
            fetched.extend(fetch_group(hits))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for chunk in pool.map(fetch_group, list(groups.values())):
                fetched.extend(chunk)

    evidences: list[Evidence] = []
    for hit, html in fetched:
        text = extract_main_text(html) if "<" in html[:2000] else html
        ev = extract_evidence(claim, hit, text, llm, organizer_hosts=organizer_hosts)
        if ev:
            evidences.append(ev)
    return evidences


def collect_for_claim(claim: Claim, candidate_name: str, searcher: Searcher, llm: LLM,
                      *, budget: Budget | None = None, fetcher: PageFetcher | None = None,
                      queries: list[Query] | None = None) -> tuple[list[Evidence], bool]:
    """返回 (证据列表, search_exhausted)。预算用完就停，绝不无限搜索。

    流程：候选人自提供链接先读 → 逐条查询检索（受搜索预算约束，`site=school` 现查
    现发现学校域）→ 把所有命中按相关性排序、跨查询轮转分配读页预算 → 不同域并发回读。
    """
    budget = budget or Budget()
    fetcher = fetcher or PageFetcher()
    queries = queries if queries is not None else plan_queries(claim, candidate_name)
    organizer_hosts = {claim.entities.get("organizer_domain")} - {None} if claim.entities else set()

    evidences: list[Evidence] = []
    seen_urls: set[str] = set()

    # 候选人自己提供的链接优先读，且不占检索预算——它不是"搜出来的"。
    # 公众号内容基本只能从这条路进来（第 7 节）。
    for url in provided_urls(claim):
        if url in seen_urls or not budget.can_read():
            continue
        seen_urls.add(url)
        budget.note_read()
        html = fetcher.get(url)
        if not html:
            continue
        text = extract_main_text(html) if "<" in html[:2000] else html
        ev = extract_evidence(claim, SearchHit(url=url, title=""), text, llm,
                              organizer_hosts=organizer_hosts, candidate_provided=True)
        if ev:
            evidences.append(ev)

    # ── 检索阶段：逐条查询，只受搜索预算约束；命中先收集，不在这里读页 ──
    per_query: list[list[SearchHit]] = []
    first_class = github_hits(claim.entities or {})   # 本人给的 GitHub 链接，一等锚点
    for q in queries:
        if not budget.can_search():
            break
        budget.note_search()
        try:
            site = q.site
            # `site=school` 是个**语义代号**：学校域名表查得到就用表里的，
            # 查不到就从宽搜结果里动态发现（缓存在机构名上）。
            if site == "school":
                org = (claim.entities or {}).get("org") or ""
                site = school_domain(org) or _discover_school_domain(org, searcher)
            hits = searcher.search(q.text, site=site)
        except Exception:
            hits = []
        # 候选人自己给的 GitHub 账号/仓库是一等锚点：不存在同名误认，直接走公共 API
        if first_class:
            hits = list(first_class) + list(hits)
            first_class = None
        per_query.append(list(hits))

    # 每条查询内部按相关性排序：供应商第一条常是噪音，别让它占了预算。
    for hits in per_query:
        hits.sort(key=lambda h: -_relevance(h, candidate_name, claim))

    # 跨查询**轮转**选页：一条查询的结果不能垄断读页预算。去重、受读页预算约束。
    selected: list[SearchHit] = []
    cursors = [0] * len(per_query)
    while budget.can_read():
        picked = False
        for qi, hits in enumerate(per_query):
            j = cursors[qi]
            while j < len(hits) and hits[j].url in seen_urls:
                j += 1
            cursors[qi] = j
            if j >= len(hits):
                continue
            hit = hits[j]
            cursors[qi] = j + 1
            seen_urls.add(hit.url)
            budget.note_read()
            selected.append(hit)
            picked = True
            if not budget.can_read():
                break
        if not picked:
            break

    evidences.extend(_read_and_extract(claim, selected, fetcher, llm, organizer_hosts))
    return evidences, budget.exhausted
