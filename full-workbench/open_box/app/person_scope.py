"""同名记录准入：来源等级、姓名、发布时间不能替代人员身份关联。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import re

from .identity import matches_school
from .plan import claim_school, school_domain, _load_schools
from .search import url_in_domain


@dataclass
class PersonCheck:
    allowed: bool
    signals: list[str] = field(default_factory=list)
    reason: str = ""


def _compact(text):
    return re.sub(r"[\W_]+", "", text or "").casefold()


def _year(text):
    m = re.search(r"(?:19|20)\d{2}", str(text or ""))
    return int(m.group()) if m else None


def _name_patterns(name):
    from .strategy import _name_variants
    patterns = []
    for n in _name_variants(name):
        if re.search(r"[\u4e00-\u9fff]", n):
            # 唐思远不是唐思；“唐思于/唐思现任”仍是有效的本人叙述。
            tail = r"(?=$|[^\u4e00-\u9fff]|于|是|为|现任|目前|担任|获得|荣获|就读|来自|同学|老师|教授|副教授|助理教授|讲师|研究员|博士|硕士|本科|参加|参与|曾|的|主编|执行主编|编著|编写|通过|攻读|毕业|进入|讲授|授课|面向|承担|在)"
            prefixes = ("本科生", "研究生", "博士生", "硕士生", "副教授", "助理教授", "教授", "讲师",
                        "研究员", "导师", "作者", "成员", "队长", "负责人", "志愿者", "同学", "学生", "的")
            head = r"(?:(?<![\u4e00-\u9fff])|" + "|".join("(?<="+p+")" for p in prefixes) + ")"
            patterns.append(re.compile(head + re.escape(n) + tail))
        else:
            patterns.append(re.compile(r"(?<![A-Za-z0-9])" + r"\s+".join(map(re.escape, n.split())) + r"(?![A-Za-z0-9])", re.I))
    return patterns


def _records(text, patterns):
    records = []
    for part in re.split(r"[\n\r；;。]", text or ""):
        for p in patterns:
            for m in p.finditer(part):
                # 单个名单行/叙述句，不能借整页页脚或其他人的学校认人。
                record = part[max(0, m.start()-180):m.end()+240].strip()
                if record and record not in records:
                    records.append(record)
    return records


FACULTY = r"助理教授|副教授|教授(?!助理)|博士生导师|副研究员|研究员(?!助理)|讲师|assistant\s+professor|associate\s+professor|professor|lecturer"


def _own_faculty_role(record, patterns):
    for p in patterns:
        for m in p.finditer(record):
            before, after = record[:m.start()], record[m.end():]
            if re.search(r"(?:"+FACULTY+r")\s*[:：]?\s*$", before, re.I):
                return True
            if re.match(r"\s*[,，:：]?\s*(?:(?:目前|现任|担任|是|为|任|is|currently)\s*)?(?:"+FACULTY+r")", after, re.I):
                return True
    # 带本人姓名的字段式记录。“导师/指导老师”字段不属于本人职称。
    return bool(re.search(r"(?<!导师)(?<!教师)职称\s*[:：=]\s*(?:"+FACULTY+r")", record, re.I))


def _undergrad_during(context, observed_year):
    records = context.get("education_records") or []
    if any(r.get("stage") in {"硕士", "博士", "研究生"} for r in records):
        return False
    # 已明示的任职经历可与第二学位同时存在，不凭常识否定用户明确填写的身份。
    if any(re.search(FACULTY, str(r.get("role") or r.get("raw", "")), re.I)
           and not re.search(r"导师|指导|师从|跟随", str(r.get("raw", "")))
           for r in context.get("employment_records", [])):
        return False
    for r in records:
        if r.get("stage") != "本科":
            continue
        years = re.findall(r"(?:19|20)\d{2}", str(r.get("date_label") or ""))
        start = _year(r.get("start")) or (int(years[0]) if years else None)
        end = _year(r.get("end")) or (int(years[-1]) if len(years)>1 else None)
        if r.get("current"):
            end = end or date.today().year
        if start and end and observed_year and start <= observed_year <= end:
            return True
        if observed_year is None and r.get("current"):
            return True
    return False


def _higher_stage_record(record, patterns):
    # 推荐/接收等是下一学段入口，不能误判成已经在读。
    if re.search(r"推免|保研|推荐免试|拟录取|保送|资格|接收", record):
        return False
    for p in patterns:
        for m in p.finditer(record):
            before, after = record[:m.start()], record[m.end():]
            if re.search(r"(?:硕士|博士)(?:研究)?生?\s*$", before):
                return True
            if re.match(r"\s*[,，:：]?\s*(?:(?:目前|现为|是|为)\s*)?(?:(?:19|20)\d{2}级\s*)?"
                        r"(?:硕士(?:研究)?生(?!导师)|博士(?:研究)?生(?!导师)|(?:硕士|博士)在读)", after):
                return True
    return False


def _specific_anchors(claim):
    ent = claim.entities or {}
    values = [ent.get(k) for k in ("title", "doi", "patent_no", "project", "team_id", "team_number", "contest", "conference")]
    values += [e.partition("=")[2] for e in claim.elements if e.partition("=")[0].strip()
               in {"论文标题", "题目", "项目", "项目名称", "专利号", "DOI", "活动名称", "作品/演出", "组织", "赛事", "队伍编号", "书名", "会议名称"}]
    return [v for v in values if isinstance(v, str) and len(_compact(v)) >= 8
            and _compact(v) in _compact(claim.raw_text)]


def _book_records(claim,text,patterns):
    """目录条目仅接受本人责任者行；相邻书目的作者或简介提到的人不能借用。"""
    if claim.category!='著作':return []
    title=(claim.entities or {}).get('title') or next((e.partition('=')[2] for e in claim.elements if e.startswith('书名=')),'')
    if not title or title not in claim.raw_text:return []
    result=[]
    short_lines=[s.strip() for s in text.splitlines() if s.strip()]
    # OPAC 在书名与责任者之间常插入馆藏/可借复本行；只跨这些字段，不跨另一本书。
    role=r'责任者|作者|主编|编著|编写|\b(?:author|editor)\b'
    metadata=r'^(?:馆藏|可借|复本|ISBN|索取号|作者|责任者|作者/编者)'
    for i,line in enumerate(short_lines):
        if len(line)>180 or _compact(title) not in _compact(line):continue
        for j in range(i+1,min(i+5,len(short_lines))):
            current=short_lines[j]
            if re.search(role,current,re.I) and any(p.search(current) for p in patterns):
                result.append('\n'.join(short_lines[i:j+1]));break
            if not re.search(metadata,current,re.I):break
    library=bool(re.search(r'索取号|责任者|正题名',text[:1200]))
    if library:
        for block in re.split(r'(?m)^[A-Z]{1,3}\d[^\n]{2,45}\s*$',text):
            lines=[s.strip() for s in block.splitlines() if s.strip()]
            if len(lines)>=2 and _compact(title) in _compact(lines[0]) and any(p.search(lines[1]) for p in patterns):
                result.append('\n'.join(lines[:2]))
    for block in re.split(r'(?m)^［书目条目[^］]*］\s*$|^序号\s*\d+\s*$',text or ''):
        if _compact(title) not in _compact(block) or len(block)>4500:continue
        authors=re.findall(r'(?:作\s*者\s*[/／]\s*编\s*者|责任者|编著者|作者)\s*[:：]\s*([^\n]{2,300})',block)
        if len(authors)==1 and any(p.search(authors[0]) for p in patterns) and re.search(r'I\s*S\s*B\s*N|出版社|出版日期',block,re.I):
            result.append(block.strip())
    return result


def check_person(claim, url, text, title="", *, published_at=None, school_domains=None, final=True):
    ent = claim.entities or {}
    context = ent.get("candidate_identity_context") or {}
    if not isinstance(context, dict) or context.get("policy") != "person_v2":
        return PersonCheck(True)  # 没有全份身份上下文的旧接口仍由其类别规则处理。
    name = ent.get("candidate_name") or context.get("candidate_name") or ""
    school_domains = school_domains or ent.get("identity_school_domains") or {}
    patterns = _name_patterns(name) if name else []
    book_records = _book_records(claim,text,patterns)
    records = book_records or _records(text, patterns)
    if claim.category=='著作' and not book_records:
        book_title=ent.get('title') or next((e.partition('=')[2] for e in claim.elements if e.startswith('书名=')),'')
        if book_title:
            records=[r for r in records if _compact(book_title) in _compact(r) and re.search(r'作者|编著|主编|编写|出版|\[M\]',r)]
    anchors = _specific_anchors(claim)
    if not records:
        # 已明确关联的团队赛果可证明团队获奖，但不能声称已经识别个人。
        team = ent.get("team_id") or ent.get("team_number")
        if claim.category == "竞赛" and team and _compact(str(team)) in _compact(text):
            return PersonCheck(True)
        return PersonCheck(False, reason="摘录未包含候选人的完整姓名及对应记录")

    orgs = list(dict.fromkeys(v for v in [*context.get("current_employers", []),
                 *context.get("education_schools", []),
                 *(r.get("org") for r in context.get("employment_records", [])),
                 *(org for org in (claim_school(claim), ent.get("company"), ent.get("org"))
                   if isinstance(org, str) and matches_school(org, claim.raw_text))]
                 if isinstance(v, str) and v))
    school_orgs = context.get("education_schools", [])
    year = _year(published_at) or _year(title)
    accepted, rejected = [], []
    for record in records:
        row_year = year or _year(record)
        if _own_faculty_role(record, patterns) and _undergrad_during(context, row_year):
            rejected.append("本人行显示教师职称，与输入中同一时期的本科在读身份无法关联")
            continue
        if _higher_stage_record(record, patterns) and _undergrad_during(context, row_year):
            rejected.append("本人行在同一时期属于硕士/博士在读，不能关联到本次本科在读身份")
            continue
        affiliation_record=record
        for school_record in _load_schools():
            for suffix in ['出版社','图书馆']:
                affiliation_record=affiliation_record.replace(school_record['name']+suffix,suffix)
        matched = [org for org in orgs if matches_school(org, affiliation_record)]
        foreign = [r["name"] for r in _load_schools() if r["name"] in affiliation_record
                   and not any(matches_school(org, r["name"]) for org in orgs)]
        specific = any(_compact(v) in _compact(record+" "+title) for v in anchors)
        if record in book_records:
            specific=True
        work_anchor=any(len(_compact(v))>=8 and _compact(v) in _compact(record) for v in context.get('authored_works',[]))
        specific=specific or work_anchor
        if foreign and not matched:
            rejected.append("本人行所属学校与本次输入的学校/任职经历不匹配")
            continue
        signals = []
        if matched:
            signals.append("学校一致" if any(org in school_orgs for org in matched) else "任职单位一致")
        # 学校官网不是身份通行证；本人行还应有就读身份/院系专业/同一活动记录。
        on_org = any((domain := school_domain(org) or school_domains.get(org)) and url_in_domain(url, domain)
                     for org in orgs)
        fields = [r.get("field", "") for r in context.get("education_fields", [])]
        field_match = any(v and _compact(v) in _compact(record) for v in fields)
        student_record = bool(re.search(r"本科|学士|硕士|博士|研究生|[12]\d{3}级|\b(?:student|undergraduate|bachelor|master|doctoral)\b", record, re.I))
        if claim.category!='著作' and not matched and on_org and (student_record or field_match or specific):
            signals.append("学校一致")
        if specific:
            signals.append("具体经历一致")
        if field_match and signals:
            signals.append("专业一致")
        if not signals:
            rejected.append("只有姓名，缺少与学校、任职或具体经历对应的身份锚点")
            continue
        accepted.append(signals)
    if not accepted or (final and rejected):
        return PersonCheck(False, reason=rejected[0] if rejected else "未关联到本人记录")
    return PersonCheck(True, list(dict.fromkeys(s for row in accepted for s in row)))


CONTENT_SIGNALS = {"学校一致", "任职单位一致", "学院一致", "专业一致", "年级一致", "队友或合作者一致", "具体经历一致"}


def validated_signals(signals, check):
    """模型声称的学校/身份相符不计分；只保留规则从本人原文核对出的信号。"""
    return list(dict.fromkeys([s for s in signals if s not in CONTENT_SIGNALS] + check.signals))


def filter_person_evidence(claim, evidence):
    if evidence.paper_record is not None:
        from .paper_identity import author_match
        return author_match(claim, evidence.paper_record).allowed
    if evidence.github_record is not None:
        return True  # 公共 API 适配器已有独立的账号和作者门禁。
    return check_person(claim, evidence.url, evidence.snippet, evidence.title,
                        published_at=evidence.published_at).allowed
