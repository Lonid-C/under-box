"""判定层（第 6.5 / 6.6 / 6.7 节）。

分工原则：LLM 只做理解和提取，**所有判定归规则**。
本文件里没有任何模型调用，输入相同则输出必然相同，可复现、可测试、可解释。
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .schema import Claim, Evidence, Status, VerifiedClaim

# --------------------------------------------------------------------------
# 6.5 身份判定
# --------------------------------------------------------------------------

WEIGHTS: dict[str, int] = {
    "学校一致": 2,
    "时间吻合": 1,            # 文章发布时间落在陈述时段内（规则派生，非模型）
    "发布方为相关机构": 1,    # 发布方/作者为陈述所涉机构（规则派生，非模型）
    "学院一致": 2,
    "专业一致": 1,
    "年级一致": 1,
    "队友或合作者一致": 2,
    "候选人自提供该链接": 2,
    "账号由候选人提供": 2,
}
CONFLICT_PENALTY = -5

# 规则派生的弱佐证信号（时间/发布方）：能把证据从 who 抬到部分证实，
# 但**不能单独把状态推到「证实(ok)」**——那一步要求真正的内容身份锚点。
CORROBORATION_SIGNALS = frozenset({"时间吻合", "发布方为相关机构"})

# identity_score 落在这个闭区间 → 必须人工复核
HUMAN_REVIEW_RANGE = (2, 3)


def identity_score(signals: list[str], conflicts: list[str]) -> int:
    """signals 里不认识的字段一律记 0 分，不臆测权重。"""
    return sum(WEIGHTS.get(s, 0) for s in signals) + CONFLICT_PENALTY * len(conflicts)


def identity_verdict(score: int, conflicts: list[str]) -> str:
    """'self' 认定为本人 / 'human' 转人工 / 'who' 身份未确认。

    宁可标身份未确认让人工去看，也不能把同名他人的记录算到候选人头上。
    """
    if conflicts:
        return "who"
    if score >= 4:
        return "self"
    if score >= HUMAN_REVIEW_RANGE[0]:
        return "human"
    return "who"


def _has_identity_anchor(ev) -> bool:
    """这条证据有没有**真实的内容身份锚点**（学校/学院/专业/队友/自提供等），
    而不只是时间/发布方这类弱佐证。用来决定它能否把状态推到「证实」。"""
    content = identity_score([s for s in ev.identity_signals
                              if s not in CORROBORATION_SIGNALS], [])
    return content >= 2 or ev.identity_score >= 4


def score_evidence(ev: Evidence) -> Evidence:
    """按规则重算 identity_score，覆盖模型给出的任何数值。"""
    ev.identity_score = identity_score(ev.identity_signals, ev.identity_conflicts)
    return ev


def _year(v: str | None) -> int | None:
    if not v:
        return None
    m = re.search(r"(19|20)\d{2}", v)
    return int(m.group(0)) if m else None


def corroboration_signals(claim, ev) -> list[str]:
    """规则派生的**弱佐证**：文章时间与陈述时段吻合、发布方/作者为陈述所涉机构。

    只把「在权威语境里、时间对得上、且提到本人」的证据从 who 抬到部分证实
    （仍在 2–3 分的人工复核带内），**不足以单独认定本人**——要配合内容锚点
    （学校/学院一致等）才到 self。冲突仍是硬否决，这里不参与。
    """
    out: list[str] = []
    py = _year(getattr(ev, "published_at", None))
    if py is not None:
        ys = _year(getattr(claim, "date_start", None)) or _year(getattr(claim, "date_label", None))
        ye = _year(getattr(claim, "date_end", None)) or _year(getattr(claim, "date_label", None)) or ys
        if ys and ye and (min(ys, ye) - 1) <= py <= (max(ys, ye) + 1):
            out.append("时间吻合")
    org = (getattr(claim, "entities", None) or {}).get("org") or ""
    host = _host(ev.url)
    pub = ev.publisher or ""
    dom = None
    if org:
        try:
            from .plan import school_domain
            dom = school_domain(org)
        except Exception:
            dom = None
    on_org_domain = bool(dom) and (host == dom or host.endswith("." + dom))
    publisher_names_org = bool(org) and len(org) >= 3 and org in pub
    if on_org_domain or publisher_names_org:
        out.append("发布方为相关机构")
    return out



# --------------------------------------------------------------------------
# 6.6 来源分级
# --------------------------------------------------------------------------

# 注：.example / .invalid 是 RFC 6761/2606 保留域名，只出现在内置的虚构 fixture 里，
# 让虚构来源也能走同一套分级规则，不会解析到任何真实站点。
SCHOOL_HOST_RE = re.compile(r"(^|\.)edu\.(cn|example)$|(^|\.)edu$")

OFFICIAL_HOSTS = {
    "chsi.com.cn",            # 学信网
    "cnipa.gov.cn",           # 国家知识产权局
    "pss-system.cponline.cnipa.gov.cn",
}
OFFICIAL_HOST_SUFFIX = (".gov.cn", ".gov.example")

PLATFORM_HOSTS = {
    "github.com", "api.github.com", "gitee.com",
    "orcid.org", "pub.orcid.org",
    "api.crossref.org", "doi.org",
    "api.openalex.org", "openalex.org",
    # fixture 用的虚构镜像
    "github.example", "api.crossref.example", "orcid.example",
}

WECHAT_HOSTS = {"mp.weixin.qq.com", "mp.weixin.example"}

# A 级还要求页面属于公示/通知/名单类
OFFICIAL_DOC_WORDS = ("公示", "通知", "公告", "名单", "决定", "表彰", "获奖", "录取", "授予")
OFFICIAL_PERSON_WORDS = ("人物专访", "人物介绍", "学子风采", "青年说", "在读", "就读",
                         "硕士研究生", "博士研究生", "研究生风采")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().lstrip(".")


def is_school_host(url: str) -> bool:
    return bool(SCHOOL_HOST_RE.search(_host(url)))


def looks_official_doc(title: str, snippet: str = "") -> bool:
    blob = f"{title} {snippet}"
    return any(w in blob for w in OFFICIAL_DOC_WORDS)


def classify_tier(
    url: str,
    title: str = "",
    publisher: str = "",
    snippet: str = "",
    *,
    organizer_hosts: set[str] | None = None,
    wechat_verified_subject: str | None = None,
    authoritative_media: bool = False,
    is_repost: bool = False,
) -> str:
    """逐条匹配，取命中的最高级。判不出来一律退到 D，不往上凑。"""
    host = _host(url)
    organizer_hosts = {h.lower() for h in (organizer_hosts or set())}

    official_site = (
        is_school_host(url)
        or host in OFFICIAL_HOSTS
        or host.endswith(OFFICIAL_HOST_SUFFIX)
        or host in organizer_hosts
        or any(host.endswith("." + h) for h in organizer_hosts)
    )
    # A：官方站点 + 公示/通知/名单类页面，且不是转载
    contest_results = (organizer_hosts and any(_host(url) == h or _host(url).endswith("." + h)
                       for h in organizer_hosts)
                       and re.search(r"\b(?:results?|winners?|awards?|standings|rankings?|scoreboard)\b",
                                     f"{title} {snippet}", re.I))
    if official_site and (looks_official_doc(title, snippet) or contest_results) and not is_repost:
        return "A"

    # B：官网本人介绍/就读报道、认证公众号文章、权威媒体报道。
    # 来源可信与是不是本人仍分开判断；普通机构简介保持 D。
    if (official_site and not is_repost
            and any(word in f"{title} {snippet}" for word in OFFICIAL_PERSON_WORDS)):
        return "B"
    if host in WECHAT_HOSTS and wechat_verified_subject:
        return "B"
    if authoritative_media:
        return "B"

    # C：平台元数据
    if host in PLATFORM_HOSTS:
        return "C"

    # 其余官方页面（如部门简介）保留为 D 级线索。
    return "D"


def best_tier(evidences: list[Evidence]) -> str | None:
    """"A" < "B" < "C" < "D"，取最好的一条。"""
    tiers = [e.source_tier for e in evidences]
    return min(tiers) if tiers else None


# --------------------------------------------------------------------------
# 去重：多篇转载不构成多重证明
# --------------------------------------------------------------------------

def dedupe_by_origin(evidences: list[Evidence]) -> list[Evidence]:
    """origin_url 相同的证据只计一次；没有 origin_url 的按自身 url 归并。

    同一组里保留"来源等级最好、身份分最高"的那条作为代表。
    """
    buckets: dict[str, Evidence] = {}
    for ev in evidences:
        key = (ev.origin_url or ev.url or "").strip() or id(ev)
        cur = buckets.get(key)
        if cur is None:
            buckets[key] = ev
            continue
        better = (ev.source_tier, -ev.identity_score) < (cur.source_tier, -cur.identity_score)
        if better:
            buckets[key] = ev
    return list(buckets.values())


# --------------------------------------------------------------------------
# 6.7 状态判定（按顺序命中即停）
# --------------------------------------------------------------------------

def decide(claim: Claim, evidences: list[Evidence]) -> Status:
    ev = dedupe_by_origin(evidences)
    if not ev:
        return "none"
    if all(e.identity_score < 2 or e.identity_conflicts for e in ev):
        return "who"
    valid = [e for e in ev if e.identity_score >= 2 and not e.identity_conflicts]
    if any(e.contradicts for e in valid):
        return "ask"
    proved = set().union(*[set(e.supports) for e in valid]) if valid else set()
    if not proved:
        return "none"
    best = min(e.source_tier for e in valid)
    # 「证实」还要求至少一条有效证据带真实内容锚点——纯时间/发布方佐证
    # 只能到「部分证实(转人工)」，不冒认同名同校的人。
    if (proved >= set(claim.elements) and best in ("A", "B")
            and any(_has_identity_anchor(e) for e in valid)):
        original_proof = set().union(*(set(e.supports) for e in valid if e.extraction_method == "text"))
        if not original_proof >= set(claim.elements):
            return "part"
        return "ok"
    return "part"


def needs_human(status: str, evidences: list[Evidence]) -> bool:
    """必须人工复核才能进最终报告。

    identity_score 的检查只看去重后真正参与结论的证据——被当作转载丢弃的副本
    对结论没有贡献，不应该把一条干净的 A 级公示拖进人工队列。
    """
    if status in ("ask", "who"):
        return True
    if any(e.extraction_method != "text" for e in dedupe_by_origin(evidences)):
        return True
    lo, hi = HUMAN_REVIEW_RANGE
    return any(lo <= e.identity_score <= hi for e in dedupe_by_origin(evidences))


def valid_evidence(evidences: list[Evidence]) -> list[Evidence]:
    ev = dedupe_by_origin(evidences)
    return [e for e in ev if e.identity_score >= 2 and not e.identity_conflicts]


def split_elements(claim: Claim, evidences: list[Evidence]) -> tuple[list[str], list[str]]:
    """拆出已证明 / 尚未证明。证明了'任职组织'不等于证明了'职务'。"""
    valid = valid_evidence(evidences)
    proved_set = set().union(*[set(e.supports) for e in valid]) if valid else set()
    proved = [e for e in claim.elements if e in proved_set]
    unproved = [e for e in claim.elements if e not in proved_set]
    return proved, unproved


LINKED_PREFIX = "［关联佐证"


def corroborate_enrollment(
    verified: list[VerifiedClaim],
    candidate_name: str,
    school_domains: dict[str, str] | None = None,
) -> list[int]:
    """用同份简历其他条目已取得的官方名单，补「学历·学校」这一个要素。

    背景（2026-10 一份真实简历实测）：本科学历按设计几乎不检索，只有"官网 + 姓名"和
    公众号两条探针，搜索索引对 PDF 名单里的人名基本搜不到，于是本科永远是
    "未找到公开记录"。可同一份简历的推免、奖学金、竞赛条目往往已经读到了
    本科学校官网上列出本人的名单——只有在读学生才会出现在推免资格、学业奖学金
    名单里，这本身就是就读的公开佐证。

    规则（全部确定性，不调模型）：
      · 只补「学校=…」一个要素，不补专业、学位、就读时间；
      · 来源证据必须是别的条目里已通过身份判定的 A/B 级证据，且未被判冲突；
      · 页面在该校官方域名下，或原文同时写了该校全称和本人姓名；
      · 证据标题前缀「关联佐证·来自 cX」，报告里看得出是借来的，不冒充专门检索；
      · 若该条学历已证明学校，或出现反证，不动它。
    返回状态被改动的条目下标，调用方据此重新生成澄清问题。
    """
    from copy import deepcopy
    from .plan import claim_school, school_domain

    school_domains = school_domains or {}
    name_parts = [p for p in re.findall(r"[㐀-鿿]{2,}|[A-Za-z]{2,}", candidate_name or "")]
    changed: list[int] = []
    for idx, vc in enumerate(verified):
        claim = vc.claim
        if claim.category != "学历":
            continue
        school = claim_school(claim)
        school_el = next((e for e in claim.elements if e.split("=", 1)[0].strip() == "学校"), None)
        if not school or not school_el or school_el in vc.proved:
            continue
        if any(e.contradicts for e in valid_evidence(vc.evidence)):
            continue
        domain = school_domain(school) or school_domains.get(school)
        borrowed: list[Evidence] = []
        seen = {e.url for e in vc.evidence}
        for other_idx, other in enumerate(verified):
            if other_idx == idx:
                continue
            for ev in valid_evidence(other.evidence):
                if ev.source_tier not in ("A", "B") or ev.url in seen:
                    continue
                if ev.title.startswith(LINKED_PREFIX):
                    continue              # 不转借转借来的证据
                host = _host(ev.url)
                on_domain = bool(domain) and (host == domain or host.endswith("." + domain))
                text = f"{ev.title} {ev.snippet}"
                names_person = not name_parts or all(p in text for p in name_parts)
                names_school = school in text
                if not names_person or not (on_domain or names_school):
                    continue
                copy = deepcopy(ev)
                copy.title = f"{LINKED_PREFIX}·来自 {other.claim.id} {other.claim.category}］{ev.title}"
                copy.supports = [school_el]
                copy.contradicts = []
                if "学校一致" not in copy.identity_signals:
                    copy.identity_signals.append("学校一致")
                borrowed.append(copy)
                seen.add(ev.url)
        if not borrowed:
            continue
        updated = assess(claim, [*vc.evidence, *borrowed], search_exhausted=vc.search_exhausted)
        updated.question = vc.question
        updated.next_step = vc.next_step
        if updated.status != vc.status:
            changed.append(idx)
        verified[idx] = updated
    return changed


def assess(
    claim: Claim,
    evidences: list[Evidence],
    *,
    search_exhausted: bool = False,
    rescore: bool = True,
) -> VerifiedClaim:
    """把一条 claim 和它的证据跑完整套规则，产出 VerifiedClaim。

    唯一的判定入口——pipeline 与 fixture 构建都走这里，保证两边结论一致。
    """
    if rescore:
        for ev in evidences:
            for sig in corroboration_signals(claim, ev):
                if sig not in ev.identity_signals:
                    ev.identity_signals.append(sig)
            score_evidence(ev)

    status: Status = "none" if search_exhausted and not evidences else decide(claim, evidences)
    proved, unproved = split_elements(claim, evidences)
    valid = valid_evidence(evidences)
    found = dedupe_by_origin(evidences)

    # best_tier 报告的是"找到的材料"的最好等级：身份未确认时材料确实存在，
    # 等级照实显示，让人工知道该去看什么；完全没有材料才是 None。
    tier = best_tier(valid) if valid else (best_tier(found) if found else None)

    return VerifiedClaim(
        claim=claim,
        status=status,
        best_tier=tier,
        evidence=evidences,
        proved=proved,
        unproved=unproved,
        needs_human=needs_human(status, evidences),
        search_exhausted=search_exhausted,
    )
