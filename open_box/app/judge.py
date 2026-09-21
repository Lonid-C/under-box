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
    "学院一致": 2,
    "专业一致": 1,
    "年级一致": 1,
    "队友或合作者一致": 2,
    "候选人自提供该链接": 2,
    "账号由候选人提供": 2,
}
CONFLICT_PENALTY = -5

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


def score_evidence(ev: Evidence) -> Evidence:
    """按规则重算 identity_score，覆盖模型给出的任何数值。"""
    ev.identity_score = identity_score(ev.identity_signals, ev.identity_conflicts)
    return ev


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
    if official_site and looks_official_doc(title, snippet) and not is_repost:
        return "A"

    # B：学校／学院／主办方认证主体的公众号文章；权威媒体报道
    if host in WECHAT_HOSTS and wechat_verified_subject:
        return "B"
    if authoritative_media:
        return "B"

    # C：平台元数据
    if host in PLATFORM_HOSTS:
        return "C"

    # 官方站点但不是公示类页面（如部门简介），降到 D 更稳
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
    if proved >= set(claim.elements) and best in ("A", "B"):
        return "ok"
    return "part"


def needs_human(status: str, evidences: list[Evidence]) -> bool:
    """必须人工复核才能进最终报告。

    identity_score 的检查只看去重后真正参与结论的证据——被当作转载丢弃的副本
    对结论没有贡献，不应该把一条干净的 A 级公示拖进人工队列。
    """
    if status in ("ask", "who"):
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
