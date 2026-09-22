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
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from . import judge
from .llm import LLM
from .parse import extract_main_text, wrap_untrusted
from .plan import (MAX_PAGE_READS, MAX_SEARCHES, MAX_SECONDS, Query,
                   claim_school, plan_queries, provided_urls, school_domain)
from .schema import Claim, Evidence, as_str_list
from .search import (crossref_hits, github_hits, PageFetcher, rephrase_variants,
                     SearchFiltered, SearchHit, Searcher, SearchUnavailable, url_in_domain)
from .strategy import _name_variants

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


def _url_key(url: str) -> str:
    p = urlparse(url)
    # 只移除明确的统计参数；保留微信的 sn/mid/idx 等标识和访问参数。
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")]
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.params,
                       urlencode(sorted(query)), ""))


def _focus_text(text: str, claim: Claim, candidate_name: str) -> str:
    """保留页首上下文及姓名/成果附近的原文，长名单尾部不再直接被截掉。"""
    if len(text) <= MAX_PAGE_CHARS:
        return text
    names = _name_variants(candidate_name or (claim.entities or {}).get("name", ""))
    terms = names + [e.split("=", 1)[-1] for e in claim.elements]
    intervals = [(0, 1500)]
    available = MAX_PAGE_CHARS - 1800
    lowered = text.lower()
    for term in dict.fromkeys(terms):
        if len(term) < 2:
            continue
        pos = 0
        for _ in range(3):
            pos = lowered.find(term.lower(), pos)
            if pos < 0 or available < 500:
                break
            start, end = max(0, pos - 700), min(len(text), pos + 1400)
            if not any(a <= pos < b for a, b in intervals):
                end = min(end, start + available)
                intervals.append((start, end))
                available -= end - start
            pos += len(term)
    if len(intervals) == 1:
        return text[:MAX_PAGE_CHARS] + "\n［正文过长，仅展示前部］"
    merged = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return "\n［中间原文省略］\n".join(text[a:b] for a, b in merged)


def extract_evidence(claim: Claim, hit: SearchHit, page_text: str, llm: LLM,
                     *, organizer_hosts: set[str] | None = None,
                     candidate_provided: bool = False, candidate_name: str = "") -> Evidence | None:
    """让模型逐字摘录，然后由规则定级和打身份分。模型不参与任何判定。"""
    system = EXTRACT_SYSTEM.format(
        raw_text=claim.raw_text, elements=claim.elements, entities=claim.entities)
    page_text = _focus_text(page_text, claim, candidate_name)
    try:
        got = llm.complete_json(system, wrap_untrusted(page_text))
    except Exception:
        return None
    if not isinstance(got, dict):
        return None

    snippet = (got.get("snippet") or "").strip()
    # 模型该给数组的地方经常给单个字符串。必须**先收敛再判断**：
    # 否则下面 `[s for s in supports if ...]` 会逐字符迭代，supports 静默变成空列表——
    # 不报错，但证据全丢。identity_conflicts 更要保住：它是"同名一票否决"的唯一依据。
    supports = as_str_list(got.get("supports"))
    conflicts = as_str_list(got.get("identity_conflicts"))
    signals = as_str_list(got.get("identity_signals"))
    contradicts = as_str_list(got.get("contradicts"))
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

    # "候选人自提供该链接"是我们自己知道的事实，不该交给模型判断——按规则补，并去重
    if candidate_provided and "候选人自提供该链接" not in signals:
        signals = [*signals, "候选人自提供该链接"]

    try:
        ev = Evidence(
            url=hit.url, title=title, publisher=publisher, source_tier=tier, snippet=snippet,
            published_at=got.get("published_at"), accessed_at=_now(),
            identity_signals=signals,
            identity_conflicts=conflicts,
            supports=supports,
            contradicts=contradicts,
            origin_url=origin_url,
        )
    except Exception:
        # 兜底：单条证据不合规就丢弃，不让一个脏字段毁掉整份报告。
        # split.py 对 Claim 早就是这么做的，这里原先漏了——
        # 一个字符串类型的 identity_conflicts 就能让整次核验直接失败。
        return None
    return judge.score_evidence(ev)


# ── 学校域动态发现 ────────────────────────────────────────────────────────
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


def _discover_school_domain(org: str, hits: list[SearchHit], *, homepage: bool = False) -> str | None:
    """复用已经计入预算的结果。域名仅是检索提示，不是身份或来源认证。"""
    if not org:
        return None
    votes: Counter[str] = Counter()
    for h in hits:
        host = _host_of(h.url)
        base = _base_domain(host)
        if not any(base.endswith("." + suffix) for suffix in _EDU_SUFFIX):
            continue
        # 别校的访问/合作新闻经常提到目标学校；只因有 .edu 域或出现次数多就限定，
        # 会把接下来所有查询锁到别校。只接受明确的机构标题/发布方提示。
        title = h.title.strip()
        if homepage:
            # 官网发现要比普通结果复用更严格：合作新闻不等于学校首页。
            title = re.sub(r"\s+", "", title)
            tail = title.removeprefix(org).strip(" -—_|·：:")
            matched = title.startswith(org) and tail in ("", "首页", "官网", "官方网站", "欢迎您", "主页")
        else:
            matched = title.startswith(org) or h.publisher.strip() == org
        if matched:
            votes[base] += 1
    if len(votes) != 1:
        return None
    return next(iter(votes))


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
                      organizer_hosts: set[str], *, candidate_name: str = "",
                      budget: Budget | None = None, progress=None) -> list[Evidence]:
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
            if budget and budget.elapsed() >= budget.max_seconds:
                budget.exhausted = True
                break
            try:
                html = hit.content or fetcher.get(hit.url)
            except Exception:
                html = None
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
    # 下载按域并发；提取始终按选页顺序，避免域分组改变有序模型桩或证据展示顺序。
    order = {hit.url: i for i, hit in enumerate(selected)}
    fetched.sort(key=lambda pair: order[pair[0].url])
    for hit, html in fetched:
        if budget and budget.elapsed() >= budget.max_seconds:
            budget.exhausted = True
            break
        text = extract_main_text(html) if "<" in html[:2000] else html
        ev = extract_evidence(claim, hit, text, llm, organizer_hosts=organizer_hosts,
                              candidate_name=candidate_name)
        if ev:
            evidences.append(ev)
    if progress:
        progress(f"    回读 {len(selected)} 页：成功 {len(fetched)}，失败/未完成 "
                 f"{len(selected) - len(fetched)}；提取证据 {len(evidences)} 条")
    return evidences


def _search_resilient(text: str, site: str | None, searcher: Searcher, org: str,
                      say) -> list[SearchHit]:
    """执行一次检索；被内容审核误拦时用同义改述重试，仍不过就抛 SearchFiltered。

    实测（2026-09-22）：智谱的 code 1301 是**概率性误伤**，同一查询串换个词序就能过，
    且同一时段 70 次连续请求全部 200（所以与限流无关）。原实现把它归到
    SearchUnavailable 直接上抛，于是一条被误伤的查询会让整次核验跑到一半就失败。
    改述的代价只有几次 1 秒限流，换来的是前面十几次搜索的结果不白费。
    """
    try:
        return searcher.search(text, site=site)
    except SearchFiltered as blocked:
        for variant in rephrase_variants(text, org)[:3]:
            try:
                hits = searcher.search(variant, site=site)
            except SearchFiltered:
                continue
            except SearchUnavailable:
                raise
            if hits:
                say(f"    ↻ 查询被内容审核误拦，改述后取回 {len(hits)} 条：{variant}")
                return hits
            say(f"    ↻ 改述已通过审核，但仍无结果：{variant}")
        raise blocked


def collect_for_claim(claim: Claim, candidate_name: str, searcher: Searcher, llm: LLM,
                      *, budget: Budget | None = None, fetcher: PageFetcher | None = None,
                      queries: list[Query] | None = None, progress=None,
                      school_domains: dict[str, str] | None = None) -> tuple[list[Evidence], bool]:
    """返回 (证据列表, search_exhausted)。预算用完就停，绝不无限搜索。

    每两次查询回读一小批结果，留出后续查询的读页名额；不再等所有搜索完成才取证。
    空结果时在原预算内放宽引号/域限定，失败则显式上报，不伪装成零结果。
    """
    budget = budget or Budget()
    fetcher = fetcher or PageFetcher()
    queries = queries if queries is not None else plan_queries(claim, candidate_name)
    say = progress or (lambda message: None)
    organizer_hosts = {claim.entities.get("organizer_domain")} - {None} if claim.entities else set()

    evidences: list[Evidence] = []
    seen_urls: set[str] = set()

    # 候选人自己提供的链接优先读，且不占检索预算——它不是"搜出来的"。
    # 公众号内容基本只能从这条路进来（第 7 节）。
    for url in provided_urls(claim):
        if _url_key(url) in seen_urls or not budget.can_read():
            continue
        seen_urls.add(_url_key(url))
        budget.note_read()
        html = fetcher.get(url)
        if not html:
            continue
        text = extract_main_text(html) if "<" in html[:2000] else html
        ev = extract_evidence(claim, SearchHit(url=url, title=""), text, llm,
                              organizer_hosts=organizer_hosts, candidate_provided=True,
                              candidate_name=candidate_name)
        if ev:
            evidences.append(ev)

    pending = deque(queries)
    attempted: set[tuple] = set()
    per_query: list[list[SearchHit]] = []
    org = (claim.entities or {}).get("org") or next(
        (e.split("=", 1)[1].strip() for e in claim.elements
         if "=" in e and e.split("=", 1)[0].strip() in ("学校", "机构", "授予单位")), "")
    school = claim_school(claim)
    # 只复用本次流水线内通过官网发现的成功结果，不跨报告持久化，也不缓存失败。
    school_domains = school_domains if school_domains is not None else {}
    school_site = school_domain(school) or school_domains.get(school)
    discovery_attempted = False
    new_queries = 0
    total_hits = 0

    def add_hits(hits):
        nonlocal total_hits
        usable = [h for h in hits if h.url.startswith(("https://", "http://"))]
        total_hits += len(usable)
        per_query.append(sorted(usable, key=lambda h: -_relevance(h, candidate_name, claim)))

    def read_batch(limit):
        selected = []
        while len(selected) < limit and budget.can_read():
            available = []
            for bucket in per_query:
                while bucket and _url_key(bucket[0].url) in seen_urls:
                    bucket.pop(0)
                if bucket:
                    available.append(bucket)
            if not available:
                break
            # 每轮各取一页，但先读更相关查询的首项；不能让早期噪声排在后期强命中前。
            available.sort(key=lambda b: -_relevance(b[0], candidate_name, claim))
            for bucket in available:
                if len(selected) >= limit or not budget.can_read():
                    break
                hit = bucket.pop(0)
                key = _url_key(hit.url)
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                budget.note_read()
                selected.append(hit)
        evidences.extend(_read_and_extract(
            claim, selected, fetcher, llm, organizer_hosts, candidate_name=candidate_name,
            budget=budget, progress=say))

    # 直接链接的 API 元数据不依赖是否生成了通用查询，也不重新抓展示页。
    if budget.can_read():
        direct = github_hits(claim.entities or {})
        if direct:
            add_hits(direct)
            read_batch(min(2, budget.max_page_reads - budget.page_reads))

    while pending and budget.can_search():
        q = pending.popleft()
        site = school_site if q.site == "school" else q.site
        discovery = False
        if q.source.startswith("school:") and q.site == "school" and not site:
            if not school:
                say("    跳过官网探针：本条未提供可识别的学校")
                continue
            if not discovery_attempted:
                discovery = True
                discovery_attempted = True
                original = q
                q = replace(q, text=f'"{school}" 官网', site=None)
            else:
                # 未确认官网时，必须保留学校名，禁止把裸姓名放到全网。
                q = replace(q, text=f'"{school}" {q.text}', site=None, source="fallback:school")
        key = (q.text, site, q.kind)
        if key in attempted:
            continue
        attempted.add(key)
        started = time.monotonic()
        try:
            hits = (crossref_hits(q.text) if q.kind == "crossref"
                    else _search_resilient(q.text, site, searcher, org, say))
        except SearchFiltered:
            # 审核误伤，改述也没过：**只跳过这一条查询**，不中止整条流水线。
            # 一条被误伤的查询不值得毁掉十分钟的核验。日志明说是"被拦"而不是"0 条"——
            # 产品底线是把未知项如实留着，不把没搜成的事伪装成搜过了。
            say(f"    ⚠️ 查询被内容审核误拦，改述重试仍失败，跳过：{q.text}")
            continue
        except SearchUnavailable:
            raise
        except Exception as exc:
            raise SearchUnavailable(f"检索执行失败（{type(exc).__name__}），请稍后重试") from exc
        # 预算按**成功执行**的查询计：被审核拦下的那条没拿到任何结果，不该占名额。
        budget.note_search()
        if site:
            hits = [h for h in hits if url_in_domain(h.url, site)]
        say(f"    检索 {budget.searches}/{budget.max_searches}：{q.text}"
            f"{(' · ' + site) if site else ''} → {len(hits)} 条，"
            f"{time.monotonic() - started:.1f} 秒")
        if discovery:
            school_site = _discover_school_domain(school, hits, homepage=True)
            if school_site:
                school_domains[school] = school_site
                say(f"    官网域名：{school} → {school_site}（包含院系子域，继续检索原陈述）")
            else:
                say(f"    未确认唯一学校官网：{school}，后续查询保留学校名")
            pending.appendleft(original)
            # 学校首页是定位线索，不当成简历陈述的证据，也不占读页名额。
            continue
        add_hits(hits)
        new_queries += 1
        if q.site == "school" and not school_site:
            school_site = _discover_school_domain(school or org, hits)

        # 不把复杂引号表达式或未知域变成硬门槛。只对空结果追加一次渐进放宽，
        # 每一次实际搜索都计入同一预算；先执行其余原始渠道，再尝试变体。
        if not hits:
            official = q.source.startswith("school:")
            if official and site and '"' in q.text:
                fallback = replace(q, text=" ".join(q.text.replace('"', " ").split()))
            elif official and site:
                fallback = replace(q, text=f'"{school}" {q.text}', site=None, source="fallback:school")
            elif q.kind == "crossref":
                fallback = replace(q, kind="web", site=None)
            elif site and len(re.findall(r"[\w\u3400-\u9fff]+", q.text)) >= 2:
                fallback = replace(q, site=None)
            elif '"' in q.text:
                fallback = replace(q, text=" ".join(q.text.replace('"', " ").split()), site=None)
            else:
                fallback = None
            if fallback and (fallback.text, fallback.site, fallback.kind) not in attempted:
                # 原渠道优先于备用拼写，防止预算里只剩同一个查询的不同版本。
                # 官网先尝试去引号、保留域名；其他回退仍排在原渠道后。
                insert_at = 0 if official and fallback.site else next(
                    (i for i, x in enumerate(pending) if x.variant), len(pending))
                pending.insert(insert_at, fallback)

        has_time_pressure = budget.elapsed() >= budget.max_seconds * 0.4
        if new_queries >= 2 or has_time_pressure or not pending:
            remaining = budget.max_page_reads - budget.page_reads
            more_searches = bool(pending) and budget.searches < budget.max_searches
            # 后面有搜索时至少留一个名额；名额只有一个时先留到查询结束统一选最相关页。
            allowance = min(2, max(0, remaining - 1)) if more_searches else remaining
            if has_time_pressure:
                allowance = max(allowance, min(1, remaining))
            read_batch(allowance)
            new_queries = 0
        if budget.max_page_reads > 0 and budget.page_reads >= budget.max_page_reads:
            budget.exhausted = bool(pending) or any(per_query)
            break

    read_batch(max(0, budget.max_page_reads - budget.page_reads))
    say(f"    本条完成：搜索 {budget.searches} 次，返回 {total_hits} 条链接，"
        f"回读 {budget.page_reads} 页，证据 {len(evidences)} 条"
        f"{'（已触及预算）' if budget.exhausted else ''}")
    return evidences, budget.exhausted
