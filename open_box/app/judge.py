"""判定层（第 6.5 / 6.6 / 6.7 节）。

分工原则：LLM 只做理解和提取，**所有判定归规则**。
本文件里没有任何模型调用，输入相同则输出必然相同，可复现、可测试、可解释。
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .schema import Claim, Evidence, ElementProof, Status, VerifiedClaim
from .academic import filter_academic_evidence, study_stage, education_scope_matches, academic_supports
from .provenance import bind_page, bind_structured, usable_provenance


def migrate_legacy_evidence(ev: Evidence, claim: Claim) -> Evidence:
    """为缺少 provenance 的旧 Evidence 构造兼容标记，不伪造引文定位。

    历史报告保留原结论用于展示，但新核验不可通过缺字段绕过原文门槛。
    此函数只在 assess 入口对 provenance_state == "legacy_unrecorded" 的证据调用。
    """
    if ev.provenance_state != "legacy_unrecorded":
        return ev
    # 旧证据标记为 legacy_unrecorded，usable_provenance 会返回 False
    # 因此这些证据不能支持新的 ok 判定，但保留在 evidence 列表中供历史展示
    ev.validation_reasons = ["旧版未记录本项校验，不能作为新核验的通过依据"]
    return ev

# --------------------------------------------------------------------------
# 6.5 身份判定
# --------------------------------------------------------------------------

WEIGHTS: dict[str, int] = {
    "学校一致": 2,
    "任职单位一致": 2,
    "时间吻合": 1,            # 文章发布时间落在陈述时段内（规则派生，非模型）
    "发布方为相关机构": 1,    # 发布方/作者为陈述所涉机构（规则派生，非模型）
    "学院一致": 2,
    "专业一致": 1,
    "年级一致": 1,
    "队友或合作者一致": 2,
    "具体经历一致": 2,
    "英文作者联合关联": 3,
    "候选人自提供该链接": 0,
    "账号由候选人提供": 0,
}
CONFLICT_PENALTY = -5

# 规则派生的弱佐证信号（时间/发布方）：能把证据从 who 抬到部分证实，
# 但**不能单独把状态推到「证实(ok)」**——那一步要求真正的内容身份锚点。
CORROBORATION_SIGNALS = frozenset({"时间吻合", "发布方为相关机构"})

# identity_score 落在这个闭区间 → 必须人工复核
HUMAN_REVIEW_RANGE = (2, 3)


def identity_score(signals: list[str], conflicts: list[str]) -> int:
    """signals 里不认识的字段一律记 0 分，不臆测权重。"""
    return sum(WEIGHTS.get(s, 0) for s in set(signals)) + CONFLICT_PENALTY * len(set(conflicts))


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
    """这条证据有没有**真实的内容身份锚点**（学校/学院/专业/队友等），
    而不只是时间/发布方这类弱佐证。用来决定它能否把状态推到「证实」。

    只算去重后的内容信号：弱佐证不参与，也绝不因为累计总分高就反推锚点。
    """
    content = identity_score([s for s in set(ev.identity_signals)
                              if s not in CORROBORATION_SIGNALS], [])
    return content >= 2


def score_evidence(ev: Evidence) -> Evidence:
    """按规则重算 identity_score，覆盖模型给出的任何数值。"""
    ev.identity_signals = list(dict.fromkeys(ev.identity_signals))
    ev.identity_conflicts = list(dict.fromkeys(ev.identity_conflicts))
    ev.identity_score = identity_score(ev.identity_signals, ev.identity_conflicts)
    if ev.paper_record is not None:
        ev.identity_score = min(ev.identity_score, 3)  # 英文姓名与中文本人仍需人工确认
    if ev.github_record is not None:
        # 规则实测的账号绑定：记录作者与候选人提供的账号一致时才是 4 分锚点；
        # 仅账号存在、记录作者未确认（如未签名 commit）时保持 2 分线索。
        ev.identity_score = 4 if ev.github_record.get("actorConfirmed") else max(ev.identity_score, 2)
    return ev


def _evidence_usable(ev: Evidence, claim: Claim) -> bool:
    from .provenance import usable_provenance
    return usable_provenance(ev, claim)


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
                         "硕士研究生", "博士研究生", "研究生风采", "教师个人主页", "导师主页")


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
    """合并同一来源：按 URL 规范化去重；规则验证过的转引簇（origin_cluster_id）按一簇计。

    模型填写的 origin_url 不是合并依据——它可以被随手编造；只有规则确认的
    origin_cluster_id（例如结构化登记给出的原始出处）才允许跨 URL 归并。
    同一组里保留"来源等级最好、身份分最高"的那条作为代表。
    """
    buckets: dict[str, Evidence] = {}
    for ev in evidences:
        from .provenance import canonical_url
        key = (canonical_url(ev.origin_cluster_id) if ev.origin_cluster_id else "") \
            or canonical_url(ev.url) or id(ev)
        cur = buckets.get(key)
        if cur is None:
            buckets[key] = ev
            continue
        better = (ev.source_tier, -ev.identity_score) < (cur.source_tier, -cur.identity_score)
        if better:
            buckets[key] = ev
    return list(buckets.values())


def _grounded_conflict(claim, ev, element):
    """Never accept free-form model accusations as negative observations.

    Rule adapters (GitHub/registry records) generate evidence-based conflicts and
    their text is kept as-is. Text pages must cite a real field/value difference,
    verbatim, with a matching time range; publication dates don't establish it.
    """
    from .provenance import locate_quote
    if ev.github_record is not None or ev.paper_record is not None:
        return bool(ev.contradicts)
    if not _evidence_usable(ev, claim) or ev.identity_score < 4 or not _has_identity_anchor(ev):
        return False
    if ev.identity_conflicts or ev.source_tier not in {"A", "B"}:
        return False
    target_year = _year(claim.date_start) or _year(claim.date_label)
    if target_year and str(target_year) not in ev.snippet:
        return False
    field, sep, expected = element.partition("=")
    if not sep:
        return False
    for value in ev.contradicts:
        actual_field, separator, actual = value.partition("=")
        if (separator and actual_field.strip() == field.strip() and actual.strip() != expected.strip()
                and actual.strip() and locate_quote(actual.strip(), ev.snippet) is not None
                and not re.search(r"未|无|没有|无法|不能", actual)):
            return True
    return False


# --------------------------------------------------------------------------
# 6.7 状态判定（按顺序命中即停）
# --------------------------------------------------------------------------

PAPER_METADATA_FIELDS = frozenset({"论文标题", "论文题目", "题目", "刊物", "期刊", "会议", "DOI",
                                   "年份", "发表年份", "刊物类型", "作者位次"})


def _accepted_for(claim: Claim, evidences: list[Evidence],
                  element: str) -> tuple[list[Evidence], list[Evidence]]:
    """某一要素的（全部候选，合格支持）。合格支持要求 provenance 可复核。"""
    candidates = [e for e in dedupe_by_origin(evidences) if element in e.supports]
    accepted = [e for e in candidates if not e.identity_conflicts and e.identity_score >= 2
                and _evidence_usable(e, claim)]
    return candidates, accepted


def _strong_for(claim: Claim, ev: Evidence, element: str) -> bool:
    """该要素达到「已证实(ok)」门槛的证据：文本 A/B 级 + 内容身份锚点；
    GitHub 需 B 级（含完整文件与 diff）且记录作者已与候选人提供的账号对应；
    论文登记字段按结构化来源另有 C 级证明路径。"""
    if ev.github_record is not None:
        return ev.source_tier == "B" and ev.github_record.get("actorConfirmed") is True
    if ev.paper_record is not None:
        return (element.partition("=")[0].strip() in PAPER_METADATA_FIELDS
                and ev.provenance_state == "structured_metadata_verified")
    return ev.source_tier in ("A", "B") and _has_identity_anchor(ev)


def _proofs(claim: Claim, evidences: list[Evidence]) -> list[ElementProof]:
    rows = []
    for element in claim.elements:
        candidates, accepted = _accepted_for(claim, evidences, element)
        conflicts = [e for e in evidences if _grounded_conflict(claim, e, element)]
        if conflicts:
            rows.append(ElementProof(element=element, state="conflicted",
                evidence_ids=[e.evidence_id for e in conflicts],
                decision_reasons=["存在同一要素的、可回读且身份有效的原文矛盾"]))
            continue
        if not accepted:
            leads = [e for e in candidates if not e.identity_conflicts and e.identity_score >= 2]
            rows.append(ElementProof(element=element, state="partial" if leads else "unverified",
                evidence_ids=[e.evidence_id for e in candidates],
                accepted_tier=min((e.source_tier for e in leads), default=None),
                identity_basis=sorted({s for e in leads for s in e.identity_signals}),
                decision_reasons=["候选材料存在，但缺少可回读、合格身份绑定或来源门槛"] if leads else ["没有合格支持"]))
            continue
        strong = [e for e in accepted if _strong_for(claim, e, element)]
        state = "verified" if strong else "partial"
        basis = strong or accepted
        reasons = ["逐要素独立支持"]
        if state == "partial":
            reasons.append("有合格支持，但未达到该要素的证实门槛")
        rows.append(ElementProof(element=element, state=state,
            evidence_ids=[e.evidence_id for e in basis],
            accepted_tier=min((e.source_tier for e in basis), default=None),
            identity_basis=sorted({s for e in basis for s in e.identity_signals}),
            decision_reasons=reasons))
    return rows


def decide(claim: Claim, evidences: list[Evidence]) -> Status:
    ev = dedupe_by_origin(evidences)
    if not ev:
        return "none"
    proofs = _proofs(claim, ev)
    if any(p.state == "conflicted" for p in proofs) or any(
            _grounded_conflict(claim, e, element) for e in ev for element in claim.elements):
        return "ask"
    if not any(p.state in {"verified", "partial"} for p in proofs):
        return "who" if any(e.identity_conflicts or e.supports and not _has_identity_anchor(e) for e in ev) else "none"
    if all(p.state == "verified" for p in proofs):
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
    """拆出已证明 / 尚未证明。证明了'任职组织'不等于证明了'职务'。

    「已证明」= 达到证实门槛，或至少有 A/B/C 级合格支持（如 GitHub C 级
    账号关联记录、结构化登记字段）；仅见于 D 类个人自述页的要素不算已证明。
    """
    proofs = _proofs(claim, evidences)
    proved = []
    for proof in proofs:
        if proof.state == "verified":
            proved.append(proof.element)
            continue
        if proof.state == "partial":
            _, accepted = _accepted_for(claim, evidences, proof.element)
            if any(e.source_tier != "D" for e in accepted):
                proved.append(proof.element)
    unproved = [e for e in claim.elements if e not in proved]
    return proved, unproved


LINKED_PREFIX = "［关联佐证"


def corroborate_enrollment(
    verified: list[VerifiedClaim],
    candidate_name: str,
    school_domains: dict[str, str] | None = None,
) -> list[int]:
    """仅复用其他学历条目中的相同学段就读记录，补「学历·学校」这一个要素。

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
        if claim.category != "学历" or not study_stage(claim):
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
            if other.claim.category != "学历":
                continue  # 获奖与就读独立取证，不能把获奖名单反向复制为学历证据。
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
                if not education_scope_matches(claim, ev.snippet, ev.title):
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
    from .claim_scope import enrich_claim, filter_supports, summarize
    from .person_scope import filter_person_evidence, check_person, validated_signals
    from .source_guard import claim_sources, evidence_excluded
    audit = getattr(evidences, "github_audit", None)
    claim = enrich_claim(claim)
    evidences = [ev for ev in evidences if not evidence_excluded(ev, claim_sources(claim)) and not ev.identity_conflicts
                 and filter_person_evidence(claim, ev) and filter_academic_evidence(claim, ev)]
    evidences = [ev.model_copy(update={"supports": filter_supports(claim, ev)}) for ev in evidences]
    evidences = [ev.model_copy(update={"supports": academic_supports(claim, ev)}) for ev in evidences]
    from .verification_agents import employment_supports, employment_record
    evidences = [ev.model_copy(update={"supports": employment_supports(claim, ev)}) for ev in evidences]
    if claim.category == "任职":
        evidences = [ev for ev in evidences if employment_record(claim, ev) or ev.contradicts]
    from .faculty_recall import book_supports,book_contradicts
    evidences = [ev.model_copy(update={'supports': book_supports(claim,ev)}) for ev in evidences]
    from .verification_agents import teaching_supports
    evidences = [ev.model_copy(update={"supports": teaching_supports(claim, ev)}) for ev in evidences]
    evidences = [ev.model_copy(update={'contradicts': book_contradicts(claim,ev)}) for ev in evidences]
    from .paper_identity import supports_for
    for ev in evidences:
        if ev.paper_record is not None:
            ev.supports = supports_for(claim, ev.paper_record)
            ev.identity_signals = ["英文作者联合关联"]
            ev.source_tier = "C"
            score_evidence(ev)
    if claim.category=='学历':
        from .identity import own_education_record
        name=claim.entities.get('candidate_name') or (claim.entities.get('candidate_identity_context') or {}).get('candidate_name','')
        evidences=[ev for ev in evidences if own_education_record(ev.snippet,ev.title,name)]
    for ev in evidences:
        if ev.github_record is not None:
            from .github_audit import tier_for_record
            ev.source_tier = tier_for_record(ev.github_record)
    if rescore:
        for ev in evidences:
            if ev.github_record is None and ev.paper_record is None and (claim.entities.get("candidate_identity_context") or {}).get("policy") == "person_v2":
                checked = check_person(claim, ev.url, ev.snippet, ev.title, published_at=ev.published_at)
                ev.identity_signals = validated_signals(ev.identity_signals, checked)
            for sig in corroboration_signals(claim, ev):
                if sig not in ev.identity_signals:
                    ev.identity_signals.append(sig)
            score_evidence(ev)

    # 旧证据迁移：标记为 legacy_unrecorded，不伪造引文定位
    evidences = [migrate_legacy_evidence(ev, claim) for ev in evidences]

    status: Status = "none" if search_exhausted and not evidences else decide(claim, evidences)
    if audit and audit.get("identityUnknown"):
        status = "who"
    if audit and audit.get("unmatchedAuthor") and not any(r.get("actorConfirmed") for r in audit.get("records", [])):
        status = "who"
    # Do not export arbitrary model contradiction text as a negative observation.
    # Rule adapters (GitHub/registry) generate evidence-based conflicts; keep those.
    for ev in evidences:
        if ev.github_record is not None or ev.paper_record is not None:
            continue
        ev.contradicts = [value for value in ev.contradicts if any(
            _grounded_conflict(claim, ev.model_copy(update={"contradicts": [value]}), element)
            for element in claim.elements)]
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
        needs_human=needs_human(status, evidences) or bool(audit and audit.get("unverifiedCommit")),
        search_exhausted=search_exhausted,
        claim_verification=summarize(claim, proved, evidences),
        github_audit=audit,
        element_proofs=_proofs(claim, evidences),
        rule_version="proof-v2",
        decision_reasons=["各要素来源等级与身份门槛独立计算；best_tier不是全部要素等级"],
    )
