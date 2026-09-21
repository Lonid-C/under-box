"""步骤 4 + 5 的提取部分：检索与页面读取（第 6.4 节），受预算硬约束。

判定不在这里做——这里只负责"把页面读出来、让模型逐字摘录"，
tier / identity / status 一律交给 judge.py。
"""
from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import judge
from .llm import LLM
from .parse import extract_main_text, wrap_untrusted
from .plan import (MAX_PAGE_READS, MAX_SEARCHES, MAX_SECONDS, Query,
                   plan_queries, provided_urls)
from .schema import Claim, Evidence
from .search import github_hits, PageFetcher, SearchHit, Searcher

# 页面正文送进模型前的上限。学校通知页常常带着整站导航，不截断会白烧 token。
MAX_PAGE_CHARS = 12000

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

    tier = judge.classify_tier(
        hit.url, title, publisher, snippet,
        organizer_hosts=organizer_hosts,
        wechat_verified_subject=got.get("wechat_verified_subject"),
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


def _discover_school_domain(org: str, searcher) -> str | None:
    """从宽搜结果里发现机构的真实域名（OSINT「递归 pivot」的最小实现）。

    为什么需要：学校域名表（data/schools.json）只有 22 所，永远追不上真实世界——
    实测一所普通高校（真实简历里出现的那种）就不在表里，导致所有 `site=school` 查询退化成全网搜。
    而且机构官网往往是一**族**子域（uic.edu.cn / sao.bnbu.edu.cn 学生事务处 /
    admission.bnbu.edu.cn 招生网），选错子域会捞回一堆招生简章。

    做法：宽搜「机构名」，统计命中结果的域名，**优先取教育/政府域**
    （.edu.cn / .edu.hk / .gov.cn …，出现 ≥2 次且非聚合站）；
    没有教育域时退回出现 ≥3 次的普通域。结果按机构名缓存。
    """
    if org in _domain_cache:
        return _domain_cache[org]
    pick: str | None = None
    try:
        doms: Counter[str] = Counter()
        for h in searcher.search(f'"{org}"', site=None):
            # 注意 URL 可能是 https://user@host/... 的形状，[^/@] 才能取对主机名
            m = re.match(r"https?://([^/@]+)/", (h.url or "") + "/")
            if m:
                doms[m.group(1).lower().replace("www.", "")] += 1
        official = [d for d, c in doms.most_common()
                    if c >= 2 and d.endswith(("edu.cn", "edu.hk", "ac.cn", "gov.cn", "org.cn", "edu"))
                    and not any(x in d for x in _AD_DOMAIN)]
        if official:
            pick = official[0]
        else:
            for d, c in doms.most_common():
                if c >= 3 and not any(x in d for x in _AD_DOMAIN):
                    pick = d
                    break
    except Exception:
        pick = None
    _domain_cache[org] = pick
    return pick


def collect_for_claim(claim: Claim, candidate_name: str, searcher: Searcher, llm: LLM,
                      *, budget: Budget | None = None, fetcher: PageFetcher | None = None,
                      queries: list[Query] | None = None) -> tuple[list[Evidence], bool]:
    """返回 (证据列表, search_exhausted)。预算用完就停，绝不无限搜索。"""
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

    first_class = github_hits(claim.entities or {})   # 本人给的 GitHub 链接，一等锚点
    for q in queries:
        if not budget.can_search():
            break
        budget.note_search()
        try:
            site = q.site
            # `site=school` 是个**语义代号**：学校域名表查得到就用表里的，
            # 查不到就从宽搜结果里动态发现（缓存在机构名上）。
            # 没有这一步，真实简历的学校几乎全部退化成全网搜。
            if site == "school":
                org = (claim.entities or {}).get("org") or ""
                site = school_domain(org) or _discover_school_domain(org, searcher)
            hits = searcher.search(q.text, site=site)
        except Exception:
            hits = []
        # 候选人自己给的 GitHub 账号/仓库是一等锚点：不存在同名误认，直接走公共 API
        if first_class is not None:
            hits = first_class + hits
            first_class = None

        for hit in hits:
            if hit.url in seen_urls:
                continue
            if not budget.can_read():
                break
            seen_urls.add(hit.url)
            budget.note_read()
            html = fetcher.get(hit.url)
            if not html:
                continue
            text = extract_main_text(html) if "<" in html[:2000] else html
            ev = extract_evidence(claim, hit, text, llm, organizer_hosts=organizer_hosts)
            if ev:
                evidences.append(ev)

    return evidences, budget.exhausted
