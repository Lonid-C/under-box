"""学段与证据用途：就读、推免/录取、授予学位和获奖分别核对。"""
from __future__ import annotations

import re

AWARD_CATEGORIES = {"奖学金", "校内荣誉"}
LEVELS = {
    "本科": r"本科(?!组)|学士|\b(?:bachelor|undergraduate)\b|\bb\.(?:sc|eng|a)\.?\b",
    "硕士": r"硕士|\bmaster|\bm\.(?:sc|eng|a)\.?\b",
    "博士": r"博士|\bph\.?\s?d\b|\bdoctoral\b",
}
# 导师学生名单常写“X专业保研/考研（某校某院）”：括号里是本科来源，前面是本科专业。
# 华工牛保庄主页即此写法（“2021级硕士 张建华 工业工程专业保研（华工工管学院）”），
# 全页没有“本科/学士”字样。硕博连读/考博后的括号是硕士阶段来源，不在此列。
UNDERGRAD_ORIGIN = re.compile(r"专业(?:保研|考研|推免|保送)\s*[（(](?P<school>[^（()）]{0,28}(?:大学|学院|学校)[^（()）]{0,10})[）)]")


def stages_in(text: str) -> set[str]:
    return {level for level, pattern in LEVELS.items() if re.search(pattern, text or "", re.I)}


def study_stage(claim) -> str:
    values = [e.partition("=")[2] for e in claim.elements if e.partition("=")[0].strip()
              in {"学位", "学历层次", "学段", "培养层次", "获奖学段"}]
    if values:
        levels = stages_in(" ".join(values))
        if len(levels) == 1:
            return next(iter(levels))
    levels = stages_in(claim.raw_text)
    if len(levels) == 1:
        return next(iter(levels))
    if not levels and "研究生" in claim.raw_text:
        return "研究生"
    # 奖项的时间范围仅作检索约束，不自动变成已证实学段。
    hint = (claim.entities or {}).get("study_stage_hint", "")
    return hint if hint in LEVELS else ""


def source_stages(snippet: str, title: str = "") -> set[str]:
    body = f"{title}\n{snippet}"
    qualification = (re.search(r"推免|推荐免试|免试攻读|拟推荐", body)
                     and re.search(r"资格|推荐名单", body)
                     and not re.search(r"拟录取|接收", title))
    if qualification and (re.search(r"资格|推荐名单", title) or not stages_in(snippet)):
        # 本人行已经写明硕士/博士时，不因正文出现“推免资格”降为本科。
        # “本科推荐免试攻读硕士资格”里的硕士是下一阶段，不是当前就读层次。
        return {"本科"}
    # “研究生国家奖学金”不能自动等同于硕士；须另有明确层次。
    # 同一份公告可能包含本硕博多个附件；本人行的层次优先于总标题。
    stages = stages_in(snippet) or stages_in(title)
    if UNDERGRAD_ORIGIN.search(snippet or ""):
        stages = stages | {"本科"}
    return stages or ({"研究生"} if "研究生" in body else set())


def education_scope_matches(claim, snippet: str, title: str = "") -> bool:
    target = study_stage(claim)
    if not target:
        return True
    if target == "本科" and "本科" not in stages_in(snippet) and UNDERGRAD_ORIGIN.search(snippet):
        from .plan import claim_school
        from .identity import matches_school
        # 括号标明的是来源院校；接收学校在标题/官网域名中出现不能代替本科来源。
        return any(matches_school(claim_school(claim), m.group("school"))
                   for m in UNDERGRAD_ORIGIN.finditer(snippet))
    observed = source_stages(snippet, title)
    if target == "研究生":
        return bool(observed & {"硕士", "博士", "研究生"})
    # 录取表里明确列“本科学校”可佐证本科来源，但不能证明研究生学位已获得。
    return target in observed


def award_name(claim) -> str:
    entities = claim.entities or {}
    values = [entities.get("award")]
    values += [e.partition("=")[2].strip() for e in claim.elements if e.partition("=")[0].strip()
               in {"奖项", "奖学金", "荣誉", "荣誉名称", "奖项名称"}]
    for value in values:
        if isinstance(value, str) and value.strip():
            name = re.sub(r"(?:19|20)\d{2}(?:[-—–至](?:19|20)\d{2})?\s*(?:学年|年度|年)?", "", value)
            name = re.sub(r"^(?:本科生?|硕士(?:研究生)?|博士(?:研究生)?|研究生)\s*", "", name.strip())
            return name.strip()
    matches = re.findall(r"(?:国家励志|国家|校长|学业|学术|优秀学生)奖学金|科研之星奖|优秀毕业生|三好学生|杰出审稿人(?:奖)?", claim.raw_text)
    return matches[0] if len(set(matches)) == 1 else ""


def _award_roster(title: str, award: str = "") -> bool:
    """明确荣誉名称加名单/公示即可；不要求重复出现“获奖”等通用词。"""
    if not re.search(r"名单|名册|公示|决定|获得者", title):
        return False
    if re.search(r"获奖|授奖|奖励|奖学金|表彰|荣誉", title):
        return True
    compact = lambda value: re.sub(r"[\s\W_]+", "", value).casefold()
    # “校级三好学生名单”“优秀毕业生公示”等以称号直接命名的正式名单。
    # 招生/报名等名单中的荣誉名称不能变成一次新的获奖记录。
    return bool(award and compact(award) in compact(title)
                and not re.search(r"申请|申报|报名|参评|候选|候补|备选|推免|录取|招生|"
                                  r"课程|培训|会议|研讨|讲座|活动|评选办法|实施细则", title))


def award_source_years(snippet: str, title: str = "", *, award: str = "") -> set[str]:
    text = re.sub(r"(?:19|20)\d{2}\s*[级届]", "", snippet)
    text = re.sub(r"(?:19|20)\d{2}年?(?:出生|生人)|(?:出生于?|born\s+in)\s*(?:19|20)\d{2}", "", text, flags=re.I)
    # 公示期/发布日期属于通知，不是本人获奖年；正式名单标题可提供评奖学年。
    text = re.sub(r"(?:公示(?:期|时间)|发布(?:时间|日期)|公告日期)[^。；;\n]*", "", text)
    years = set(re.findall(r"(?:19|20)\d{2}", text))
    if years:
        return years
    # 年级、文章发布时间、个人主页年份不充当获奖年度；仅正式评奖名单标题可补表头。
    if _award_roster(title, award):
        return set(re.findall(r"(?:19|20)\d{2}", title))
    return set()


def award_scope_matches(claim, snippet: str, title: str = "", candidate_name: str = "") -> bool:
    """只有本人获目标奖的记录才准入；不能靠同校就读、推免或页面页脚补奖。"""
    target = award_name(claim)
    if not target or not snippet:
        return False
    compact = lambda t: re.sub(r"[\s\W_]+", "", t or "").casefold()
    body = f"{title}\n{snippet}"
    if compact(target) not in compact(body):
        return False
    stage = study_stage(claim)
    observed = source_stages(snippet, title)
    if stage and observed and stage not in observed:
        return False
    target_years = set(re.findall(r"(?:19|20)\d{2}", claim.date_label)) or set(re.findall(r"(?:19|20)\d{2}", claim.raw_text))
    observed_years = award_source_years(snippet, title, award=target)
    if target_years and observed_years and not target_years.intersection(observed_years):
        return False
    if candidate_name:
        from .strategy import _name_variants
        if not any(compact(n) in compact(snippet) for n in _name_variants(candidate_name)):
            return False
        # 同一导师名单中的另一学生获奖，不能移到候选人名下。
        own = [part for part in re.split(r"[；;。\n]", snippet)
               if any(compact(n) in compact(part) for n in _name_variants(candidate_name))]
        personal_text = "\n".join(own)
    else:
        personal_text = snippet
    # 名单/本人获奖叙述；申请条件、招生奖励政策不属于本人获奖记录。
    roster = _award_roster(title, target)
    personal_award = bool(compact(target) in compact(personal_text)
                          and re.search(r"获(?:得|评|奖)?|荣获|授予|获得者|award(?:ed)?|recipient", personal_text, re.I))
    if not (roster or personal_award):
        return False
    if re.search(r"拟推荐|拟获奖|拟授予|候选|候补|备选", title) and not re.search(r"拟推荐|拟获奖|拟授予|候选|候补|备选", claim.raw_text):
        return False
    if re.search(r"申请|申报|评选办法|实施细则|招生政策|奖励政策", title) and not personal_award:
        return False
    return True


def filter_academic_evidence(claim, evidence, *, candidate_name=""):
    """统一入口供抽取、判定及复用调用，防止绕过准入条件。"""
    if claim.category == "学历":
        return education_scope_matches(claim, evidence.snippet, evidence.title)
    if claim.category in AWARD_CATEGORIES:
        from .plan import claim_school
        from .identity import education_school_matches
        return (award_scope_matches(claim, evidence.snippet, evidence.title, candidate_name)
                and (not claim_school(claim) or education_school_matches(claim, evidence.url, evidence.snippet, evidence.title)))
    return True


def academic_supports(claim, evidence) -> list[str]:
    """模型不能把推荐资格升为录取，或把未知奖项年份当作已证实。"""
    supports = list(evidence.supports)
    body = evidence.snippet + " " + evidence.title
    if claim.category == "学历":
        qualification = (re.search(r"资格|推荐名单", body) and re.search(r"推免|免试|推荐", body)
                         and not re.search(r"接收|拟录取|录取名单", evidence.title))
        if qualification:
            supports = [s for s in supports if s.partition("=")[0].strip() not in {"入学方式", "录取方式", "已录取", "学位"}]
        stage_record = re.search(r"拟录取|拟推荐|推免|保研|保送|直博|硕博连读|[12]\d{3}级|在读|博士生|硕士生|研究生|\b(?:student|admitted|enrolled)\b", evidence.snippet, re.I)
        awarded = re.search(r"(?:获[得]?|授予|取得|毕业)[^。；;\n]{0,25}(?:学位|学士|硕士|博士)|(?:学士|硕士|博士)[^。；;\n]{0,12}毕业|\b(?:earned|awarded|obtained|graduated)\b", evidence.snippet, re.I)
        if stage_record and not awarded:
            supports = [s for s in supports if s.partition("=")[0].strip()
                        not in {"学位", "毕业时间", "毕业年份", "获得学位时间", "获得学位年份"}]
        # 只凭“保研（某校某院）”认出的本科来源：证明在哪读、读什么，不证明学位与起止时间。
        if (study_stage(claim) == "本科" and UNDERGRAD_ORIGIN.search(evidence.snippet)
                and "本科" not in stages_in(evidence.snippet)):
            supports = [s for s in supports if s.partition("=")[0].strip()
                        in {"学校", "院系", "学院", "专业", "学段", "学历层次", "培养层次"}]
    elif claim.category in AWARD_CATEGORIES:
        years = award_source_years(evidence.snippet, evidence.title, award=award_name(claim))
        supports = [s for s in supports if s.partition("=")[0].strip()
                    not in {"年份", "学年", "年度", "获奖年份", "获奖时间", "获奖学年", "获评年份"}
                    or set(re.findall(r"(?:19|20)\d{2}", s.partition("=")[2])).issubset(years)]
    return supports


def verification_phase(claim) -> int:
    if claim.category == "学历":
        return {"本科": 0, "硕士": 1, "博士": 2}.get(study_stage(claim), 3)
    return 4


def with_academic_scope_context(claims):
    """按输入明确的学校/学段/时间给奖项检索加范围，不新增已核实事实。"""
    from .plan import claim_school
    records = []
    for c in claims:
        if c.category != "学历":
            continue
        years = re.findall(r"(?:19|20)\d{2}", f"{c.date_start or ''} {c.date_end or ''} {c.date_label} {c.raw_text}")
        records.append({"claim_id": c.id, "school": claim_school(c), "stage": study_stage(c),
                        "start": c.date_start, "end": c.date_end,
                        "years": sorted(set(int(y) for y in years))})
    result = []
    for c in claims:
        entities = {**(c.entities or {}), "education_history_context": records}
        elements = list(c.elements)
        if c.category in AWARD_CATEGORIES:
            fields = {e.partition("=")[0].strip() for e in elements}
            explicit_stage = study_stage(c)
            if explicit_stage and stages_in(c.raw_text) and "获奖学段" not in fields:
                elements.append("获奖学段=" + explicit_stage)
            years = set(re.findall(r"(?:19|20)\d{2}", c.date_label))
            if (len(years) == 1 and next(iter(years)) in c.raw_text
                    and not fields.intersection({"年份", "学年", "年度", "获奖年份", "获奖时间", "获奖学年"})):
                elements.append("获奖年份=" + next(iter(years)))
        if c.category in AWARD_CATEGORIES and not study_stage(c):
            years = {int(y) for y in re.findall(r"(?:19|20)\d{2}", c.date_label + " " + c.raw_text)}
            school = claim_school(c)
            possible = {r["stage"] for r in records if r["school"] == school and r["stage"] in LEVELS
                        and len(r["years"]) >= 2 and years
                        and min(years) >= min(r["years"]) and max(years) <= max(r["years"])}
            if len(possible) == 1:
                entities["study_stage_hint"] = next(iter(possible))
        result.append(c.model_copy(update={"entities": entities, "elements": elements}))
    return result


def with_verified_education_context(claim, verified):
    """后续阶段可复用已确认的就读锚点，不复用奖项或未证实的学位。"""
    from .plan import claim_school
    records = []
    for vc in verified:
        if vc.claim.category != "学历":
            continue
        school = claim_school(vc.claim)
        if not any(e.partition("=")[0].strip() == "学校" for e in vc.proved):
            continue
        records.append({"claim_id": vc.claim.id, "school": school, "stage": study_stage(vc.claim),
                        "proved_elements": vc.proved, "unproved_elements": vc.unproved,
                        "evidence_urls": [ev.url for ev in vc.evidence if ev.identity_score >= 2]})
    entities = {**(claim.entities or {}), "verified_education_context": records}
    # 已确认的本科来源可引导推免检索；同校保研也需要这条路径。它不是录取证明。
    if (claim.category == "学历" and study_stage(claim) != "本科"
            and re.search(r"保研|保送|推免|推荐免试|直博", claim.raw_text)
            and not entities.get("source_school_hint")):
        sources = {r["school"] for r in records if r["stage"] == "本科"}
        if len(sources) == 1:
            entities["source_school_hint"] = next(iter(sources))
            entities["source_study_stage_hint"] = "本科"
    return claim.model_copy(update={"entities": entities})
