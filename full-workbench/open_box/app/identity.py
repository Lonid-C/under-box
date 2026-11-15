"""本人识别的检索上下文及学历证据准入规则；上下文不是已核实事实。"""
from __future__ import annotations

import re

from .plan import claim_school, school_aliases, school_domain, _load_schools
from .search import url_in_domain


def matches_school(school: str, text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "").casefold()
    for alias in school_aliases(school):
        if re.search(r"[A-Za-z]", alias):
            words = re.findall(r"[A-Za-z0-9]+", alias)
            if words and re.search(r"(?<![A-Za-z0-9])" + r"[\W_]*".join(map(re.escape, words))
                                   + r"(?![A-Za-z0-9])", text or "", re.I):
                return True
        elif re.sub(r"\s+", "", alias).casefold() in compact:
            return True
    return False


def education_school_matches(claim, url: str, snippet: str, title: str = "",
                             school_domains: dict[str, str] | None = None) -> bool:
    """只认本人摘录或对应学校官方名单的上下文，不靠模型声称“学校一致”。"""
    school = claim_school(claim)
    schools = [school] if school else []
    # 推免推荐资格允许来源本科院校，但不能借此证明目标学校学位或入学。
    if any(word in (claim.raw_text or "") for word in ("推免", "保研", "保送", "推荐免试")):
        source = (claim.entities or {}).get("source_school_hint")
        if isinstance(source, str) and source:
            schools.append(source)
    if not schools or not snippet:
        return False
    if any(matches_school(target, snippet) for target in schools):
        return True
    # 同校官网也可能报道校外人员。摘录明确属于另一学校时，域名或页脚不能补救。
    if any(record["name"] in snippet
           and not any(matches_school(target, record["name"]) for target in schools)
           for record in _load_schools()):
        return False
    roster_title = bool(re.search(r"名单|公示|录取|推免|毕业|奖学金|在读|学籍|招生|\b(?:graduates?|admission|enrollment|roster)\b",
                                 title or "", re.I))
    if roster_title and any(matches_school(target, title) for target in schools):
        return True
    domains = school_domains or {}
    return any(domain and url_in_domain(url, domain) for target in schools
               if (domain := school_domain(target) or domains.get(target)))


def education_record_relevant(snippet: str, title: str = "") -> bool:
    """期刊名称/发布学校不是就读关系；摘录须包含教育身份或名单行信息。"""
    if re.search(r"学士|本科|硕士|博士|研究生|学籍|学历|学位|就读|毕业|入学|录取|保研|保送|推免|免试|"
                 r"硕博连读|直博|联合培养|\b(?:bachelor|master|ph\.?d|doctoral|undergraduate|student|"
                 r"education|graduated|enrolled|degree)\b", snippet or "", re.I):
        return True
    roster = re.search(r"名单|公示|名册|\b(?:roster|list)\b", title or "", re.I)
    # 表头在标题里时，保留有院系/专业/届别的本人行；只列一个姓名仍不够。
    return bool(roster and re.search(r"学院|专业|年级|[12]\d{3}[级届]|学籍|学生|\b(?:department|major|cohort)\b",
                                    snippet or "", re.I))


def own_education_record(snippet, title='', candidate_name=''):
    """导师任教单位/所教学生学段，不能证明导师本人就读或毕业。"""
    from .person_scope import _name_patterns,_records,_own_faculty_role
    patterns=_name_patterns(candidate_name) if candidate_name else []
    rows=_records(snippet,patterns) if patterns else [snippet]
    explicit=re.compile(r'毕业|就读|攻读|入学|学籍|学历|学位|获.*(?:学士|硕士|博士)|graduat|enroll|degree|education',re.I)
    return any(education_record_relevant(row,title) and (
        explicit.search(row) or not _own_faculty_role(row,patterns) and not re.search(
            r'硕士(?:研究)?生导师|博士(?:研究)?生导师|本科[^。\n]{0,25}教师|教授[^。\n]{0,25}课程|讲授[^。\n]{0,25}课程',row)) for row in rows)


def verbatim_excerpt(snippet: str, original: str) -> bool:
    """允许用显式省略号连接表头与本人行，每段须按原文顺序逐字存在。"""
    compact = lambda text: re.sub(r"\s+", "", text or "").casefold()
    text = compact(original)
    parts = [compact(part) for part in re.split(r"\[\s*…\s*\]|…+|\.{3,}|［中间原文省略］", snippet or "")]
    parts = [part for part in parts if part]
    if not parts:
        return False
    start = 0
    for part in parts:
        pos = text.find(part, start)
        if pos < 0:
            return False
        start = pos + len(part)
    return True


def has_candidate_anchor(claim, url: str, text: str, title: str = "",
                         school_domains: dict[str, str] | None = None) -> bool:
    """学生、教师、职场人都检查本人记录，而不是靠模型给的身份分。"""
    entities = claim.entities or {}
    context = entities.get("candidate_identity_context") or {}
    if isinstance(context, dict) and context.get("policy") == "person_v2":
        from .person_scope import check_person
        return check_person(claim, url, text, title, school_domains=school_domains, final=False).allowed
    if not isinstance(context, dict) or not context.get("current_employers"):
        return True
    organizations = [*context.get("current_employers", []),
                     *context.get("education_schools", []), claim_school(claim),
                     entities.get("company"), entities.get("org")]
    body = f"{title}\n{text}"
    for org in organizations:
        if not isinstance(org, str) or not org:
            continue
        domain = school_domain(org) or (school_domains or {}).get(org)
        if matches_school(org, body) or domain and url_in_domain(url, domain):
            return True
    compact = re.sub(r"[\W_]+", "", body).casefold()
    values = [entities.get(key) for key in ("title", "doi", "patent_no", "repo", "project", "team_id")]
    values += [element.partition("=")[2] for element in claim.elements
               if element.partition("=")[0].strip() in {"论文标题", "题目", "项目", "专利号", "DOI", "仓库"}]
    for value in values:
        if isinstance(value, (str, int)):
            anchor = re.sub(r"[\W_]+", "", str(value)).casefold()
            if len(anchor) >= 8 and anchor in compact:
                return True
    return False


def with_candidate_identity_context(claims: list, candidate_name: str = "") -> list:
    """把未勾选的现任经历也传给各条取证，避免只凭大众姓名认人。"""
    employers, current_jobs, education, education_fields, collaborators = [], [], [], [], []
    education_records, employment_records, works = [], [], []
    for claim in claims:
        if claim.category=='著作':
            title=(claim.entities or {}).get('title') or next((e.partition('=')[2] for e in claim.elements if e.startswith('书名=')),'')
            if title and title in claim.raw_text:
                works.append(title)
        if claim.category == "论文":
            for key in ("coauthors", "authors", "collaborators"):
                value = (claim.entities or {}).get(key)
                if isinstance(value, str):
                    collaborators.extend(re.split(r"[,，;；、]|\band\b|&", value))
                elif isinstance(value, list):
                    collaborators.extend(v for v in value if isinstance(v, str))
            for names in re.findall(r"\(\s*With\s+([^()]+)\)", claim.raw_text, re.I):
                collaborators.extend(re.split(r",|\band\b|&", names))
        if claim.category == "学历":
            school = claim_school(claim)
            if school and not matches_school(school, claim.raw_text):
                school = ""  # 模型猜测的学校不是输入中的本人身份事实。
            if school:
                education.append(school)
            from .academic import study_stage, stages_in
            literal_stages = stages_in(claim.raw_text)
            stage = next(iter(literal_stages)) if len(literal_stages) == 1 else study_stage(claim)
            education_records.append({"school": school, "stage": stage,
                                      "start": claim.date_start, "end": claim.date_end,
                                      "date_label": claim.date_label,
                                      "current": bool(re.search(r"至今|在读|目前|当前|present|currently", claim.raw_text + " " + claim.date_label, re.I))})
            fields = [v for k, v in (claim.entities or {}).items() if k in {"major", "dept"} and isinstance(v, str)]
            fields += [e.partition("=")[2].strip() for e in claim.elements
                       if e.partition("=")[0].strip() in {"专业", "院系", "学院"}]
            for value in dict.fromkeys(fields):
                if value and re.sub(r"\s+", "", value) in re.sub(r"\s+", "", claim.raw_text):
                    education_fields.append({"school": school, "field": value})
        if claim.category != "任职":
            continue
        entities = claim.entities or {}
        org = entities.get("company") or entities.get("org")
        if not isinstance(org, str) or not org.strip():
            org = next((element.partition("=")[2].strip() for element in claim.elements
                        if element.partition("=")[0].strip() in {"公司", "单位", "机构", "任职单位", "学校"}), "")
        if org and not matches_school(org, claim.raw_text):
            org = ""
        employment_records.append({"org": org, "role": entities.get("role") or "",
                                   "raw": claim.raw_text, "start": claim.date_start, "end": claim.date_end})
        if org:
            if re.search(r"现任|目前|当前|至今|现在|\b(?:present|current|currently)\b",
                         f"{claim.raw_text} {claim.date_label}", re.I):
                employers.append(org)
                current_jobs.append(claim.raw_text)
    context = {"policy": "person_v2", "candidate_name": candidate_name,
               "current_employers": list(dict.fromkeys(employers)),
               "current_employment_claims": list(dict.fromkeys(current_jobs)),
               "employment_records": employment_records, "education_records": education_records,
               "authored_works": list(dict.fromkeys(works)),
               "education_schools": list(dict.fromkeys(education)),
               "education_fields": education_fields,
               "research_collaborators": list(dict.fromkeys(v.strip() for v in collaborators if v.strip()))[:20]}
    from .academic import with_academic_scope_context
    return with_academic_scope_context([claim.model_copy(update={"entities": {**(claim.entities or {}),
                                                  "candidate_identity_context": context}})
            for claim in claims])
