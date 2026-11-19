"""ICT discovery routes. Search hints never change the claim or proof rules.

Prefer identifiers, then short work titles, then official rosters. A discovery
match still goes through the ordinary source, person, year and evidence checks.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse


def clean_anchor(value: str) -> str:
    """Remove extraction commentary, preserving meaningful campus qualifiers."""
    value = str(value or "")
    value = re.sub(r"[（(]([^（）()]*)[）)]", lambda m: "" if re.search(
        r"据.*推断|原文未|推测|待核实|未明写|根据.*推断", m[1]) else m[0], value)
    return re.sub(r"\s+", " ", value).strip()


def clean_query(value: str) -> str:
    value = clean_anchor(value).translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"}))
    value = re.sub(r'"{2,}', '"', value)
    # Only identical adjacent quoted anchors, never individual words in a title.
    value = re.sub(r'("[^"\n]+")\s+\1(?=\s|$)', r'\1', value)
    return value.strip()


def _field(claim, entity_keys, element_keys):
    values = [claim.entities.get(k) for k in entity_keys]
    values += [e.partition('=')[2].strip() for e in claim.elements
               if e.partition('=')[0].strip() in element_keys]
    return next((clean_anchor(v) for v in values if isinstance(v, str) and v.strip()), '')


def identifiers(claim):
    """Use verbatim, labelled work identifiers; never infer a publication ID."""
    raw = claim.raw_text or ''
    values = []
    if claim.category == '项目':
        values += re.findall(r'(?:项目|课题|基金|资助)(?:编号|号)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9.-]{4,39})', raw)
        v = _field(claim, ('project_no', 'project_id', 'grant_no'), ('项目编号', '课题编号', '基金编号'))
        if v and v in raw: values.append(v)
    if claim.category == '专利':
        values += re.findall(r'(?<![A-Za-z0-9])(?:CN|ZL)\s*\d{8,12}(?:\.\d|[A-Z]\d?)?(?![A-Za-z0-9])', raw, re.I)
    return list(dict.fromkeys(v.strip() for v in values if re.search(r'\d', v)))


def _project_topic(claim, entity_keys, element_keys, original_names=()):
    raw = _compact(claim.raw_text)
    values = [claim.entities.get(k) for k in entity_keys]
    values += [e.partition('=')[2] for e in claim.elements
               if e.partition('=')[0].strip() in element_keys]
    values.extend(original_names)
    for value in values:
        if not isinstance(value, str):
            continue
        value = clean_anchor(value).strip('“”"《》')
        key = _compact(value)
        if (3 <= len(key) <= 100 and key in raw and key not in {
                '项目组', '课题组', '实验室', '研究组', '项目团队', '科研团队',
                '科研项目', '校级项目', '省级项目', '课题研究', '团队项目',
                '成员名单', '项目成员', '课题成员', '团队成员', '项目负责人', '项目参与者',
        }):
            return value
    return ''


def _compact(value):
    return re.sub(r'\W', '', value or '').casefold()


def _project_topics(claim):
    project = _project_topic(claim, ('project', 'title'), ('项目名称', '课题名称', '项目', '课题'))
    # Existing split records may have the group only in the original excerpt.
    original_groups = re.findall(
        r'(?:加入\s*[“《]?|(?:所属)?(?:项目组|课题组|实验室|团队)\s*[:：]\s*[“《]?)'
        r'([^，。；、：,;\n（）()“”《》]{2,50}?(?:项目组|课题组|实验室|研究组|研究团队|科研团队))',
        claim.raw_text or '')
    group = _project_topic(claim, ('research_group', 'group', 'lab', 'team'),
                           ('项目组', '课题组', '团队', '实验室', '所属课题组', '所属实验室', '所属团队'),
                           original_groups)
    return project, group


def discovery_queries(claim, name, ctx):
    from .strategy import Query
    rows = []

    def add(text, route, purpose, site=None):
        if text.strip(): rows.append(Query(clean_query(text), site=site, round=1,
            weight=160, source='recovery:' + route, purpose=purpose))

    for number in identifiers(claim):
        add('"' + number + '"', 'identifier',
            '先用原文唯一编号跨来源定位；资助说明不证明申请人、经费或结题，须继续核对本人和项目状态')
        if claim.category == '专利':
            bare = re.sub(r'^(?:CN|ZL)\s*', '', number, flags=re.I)
            add('"' + bare + '" 申请 专利', 'application_number',
                '区分申请号与公开号；只从真实公开记录解析公开号，不猜造专利链接')

    title = ctx.get('paper', '')
    if (claim.category == '论文' and title
            and re.sub(r'\W', '', title).casefold() in re.sub(r'\W', '', claim.raw_text).casefold()):
        # A short distinctive prefix survives punctuation and subtitle revisions.
        # Keywords are discovery only: they do not establish title equivalence.
        prefix = title.split(':', 1)[0].strip()
        tokens = re.findall(r'[A-Za-z][A-Za-z0-9-]*', prefix)
        distinctive = [w for w in tokens if w.casefold() not in {'and', 'the', 'a', 'an', 'of', 'for', 'by', 'to', 'in'}]
        anchor = ' '.join('"' + w + '"' for w in distinctive) if ':' in title and 1 <= len(distinctive) <= 4 else ''
        if not anchor:
            words = re.findall(r'[A-Za-z][A-Za-z0-9-]*', title)
            words = [w for w in words if w.casefold() not in {'and', 'the', 'a', 'an', 'of', 'for', 'by', 'to', 'in', 'with'}]
            if len(words) >= 8: anchor = ' '.join(words[:10])
        if anchor:
            add(anchor, 'title_keywords', '用原题名短锚点查版本、DOI及出版方；题名差异、预印本与期刊年份分别保留，不能自动视为同一成果')
            from .paper_identity import latin_names
            aliases = latin_names(name)
            if aliases:
                add(anchor + ' "' + aliases[0] + '"', 'author_alias',
                    '完整英文姓名只用于发现；身份仍需同篇署名、本人单位及合作者交叉关联，拼音或首字母不能独立证明')

    school = clean_anchor(ctx.get('school', ''))
    if claim.category == '项目':
        project, group = _project_topics(claim)
        school_term = f' "{school}"' if school else ''
        site = 'school' if school else None
        if name:
            for topic in dict.fromkeys(filter(None, (project, group))):
                add(f'"{name}" "{topic}"', 'project_person',
                    '用姓名与原文项目或课题组全称查同一条人员记录，核对所属单位和任期')
        if project:
            add(f'"{project}"{school_term} {ctx.get("year", "")} 公示 名单', 'project_notice',
                '先找正式立项或结题公示，再读名单及附件，分开核对项目状态和本人角色', site=site)
        if group or project:
            add(f'"{group or project}"{school_term} 成员', 'project_roster',
                '姓名可能只在课题组成员页或名单附件，进入原文核对姓名、归属与时间', site=site)
    if claim.category == '竞赛' and not school:
        from .source_guard import campus_school_hint
        school = clean_anchor(claim.entities.get('school_search_hint', '') or campus_school_hint(claim))
    if claim.category == '竞赛' and school and name:
        add(f'"{name}" "{school}"', 'person_school',
            '跨官方索引发现含本人和学校的名单附件；仍逐项核对本条赛事、年份、奖级和角色，其他比赛不能替代')
    if claim.category in {'校内荣誉', '奖学金'} and school:
        if name:
            add(f'"{name}" "{school}" ' + ('奖学金' if claim.category=='奖学金' else '荣誉 表彰'),
                'person_school_award', '先定位本人同校公开评奖入口，再分别核对本条奖名、年度和次数；其他奖项不能移用')
        award = clean_anchor(ctx.get('award') or ctx.get('term') or '')
        if award and re.sub(r'\W', '', award) in re.sub(r'\W', '', claim.raw_text):
            year = ctx.get('year', '')
            stage = ctx.get('study_stage', '')
            add(f'"{award}" {year} {stage} 名单 公示', 'award_roster',
                '姓名可能仅在附件：先查同校同奖公告，再读取名单本人行；公告或名额不能代替个人获奖', site='school')
    if claim.category == '交流活动' and school:
        program = _field(claim, ('program',), ('项目/身份', '交流项目', '项目名称'))
        if program and program in claim.raw_text:
            quoted = re.search(r'[“"]([^”"]{2,30})[”"]', program)
            term = quoted.group(1) if quoted else re.sub(r'(成员|学员|参与者)$', '', program).strip()
            if term:
                add(f'{school} {term} {ctx.get("year", "")} 名单', 'program_membership',
                    '单查原文明确的培养项目或成员身份，回读官网。项目名单与外访本人参与须分别核对', site='school')
    return rows


def student_wechat_queries(claim, name, school):
    """Search public account indexes by role, and find rosters without a name in the title."""
    from .retrieval_repairs import role_variants
    from .strategy import Query, WECHAT_HOST

    compact = lambda value: re.sub(r'\W', '', value)
    raw = compact(claim.raw_text)
    school = clean_anchor(school)
    if compact(school) not in raw:
        school = ''
    orgs = [e.partition('=')[2].strip() for e in claim.elements
            if e.partition('=')[0].strip() in {'任职组织', '组织', '社团', '学生组织'}]
    orgs += [clean_anchor(claim.entities.get('org', ''))]
    orgs = list(dict.fromkeys(org for org in orgs if org != school and 2 <= len(org) <= 40
                              and compact(org) and compact(org) in raw))[:3]
    anchor = school or next(iter(orgs), '')
    if not anchor:
        return []

    lookups = [(f'{anchor} {name} {role}', '按学校、姓名和一项职务查公众号公开报道')
               for role in role_variants(claim) if name]
    lookups += [(f'{school} {org} 换届', '按学校与学生组织找换届名单，再核对本人及任期')
                for org in orgs]
    return [Query(clean_query(text), site=WECHAT_HOST, round=1, weight=115,
                  source='wechat:student_work', purpose=purpose) for text, purpose in lookups]


def refine_queries(claim, name, ctx, queries):
    """One deterministic set of routes, deduplicated before any paid search."""
    from dataclasses import replace
    from .strategy import WECHAT_HOST

    if claim.category == '学生工作':
        wechat = student_wechat_queries(claim, name, ctx.get('school', ''))
        if wechat:
            queries = wechat + [q for q in queries if q.site != WECHAT_HOST]
    result = []; seen = set()
    for q in discovery_queries(claim, name, ctx) + queries:
        q = replace(q, text=clean_query(q.text))
        key = (q.text, q.site, q.kind)
        if q.text and key not in seen:
            seen.add(key); result.append(q)
    return result


def identifier_feedback(claim, query, hits):
    """An engine may ignore quoted IDs and return unrelated fuzzy matches."""
    if query.source != 'recovery:identifier': return hits, []
    from .strategy import Query
    number = query.text.strip('"')
    wanted = re.sub(r'[^a-z0-9]', '', number.casefold())
    pattern = r'(?<![a-z0-9])' + r'[\s.\-]*'.join(re.escape(c) for c in wanted) + r'(?![a-z0-9])'
    matched = [h for h in hits if wanted and re.search(pattern, ' '.join((h.title or '', h.snippet or '', h.url or '', h.content or '')).casefold())]
    if matched: return matched, []
    follow = []
    if claim.category == '项目':
        topic = next((t for t in ('教学改革', '研究生创新', '大学生创新', '科研') if t in claim.raw_text), '项目')
        for text in (f'"{number}" {topic}', f'"{number}" grant funding'):
            follow.append(Query(text, round=2, source='recovery:identifier_context',
                purpose='编号精确搜索未返回对应记录，加入项目主题/英文资助语境补查；须回读原文确认编号与本人关系'))
    return matched, follow


def school_record_links(html, page, claim, school_domains=None):
    """Follow matching pages and documents on the same institution's official site."""
    if claim.category not in {'项目', '奖学金', '校内荣誉', '学生工作', '交流活动'} or '<' not in html[:2000]: return []
    from bs4 import BeautifulSoup
    from .plan import claim_school, school_domain
    from .search import SearchHit, url_in_domain
    school = claim_school(claim)
    domain = school_domain(school) or (school_domains or {}).get(school)
    if not domain or not url_in_domain(page.url, domain): return []
    fields = {'项目': (('project', 'title'), ('项目名称', '项目')),
              '奖学金': (('award',), ('奖项', '奖学金', '奖项名称')),
              '校内荣誉': (('award',), ('荣誉', '奖项', '荣誉名称')),
              '学生工作': (('org',), ('任职组织', '组织')),
              '交流活动': (('program',), ('项目/身份', '交流项目', '项目名称'))}
    keys, labels = fields[claim.category]; topic = _field(claim, keys, labels)
    topic = topic.strip('“”"')
    body = BeautifulSoup(html, 'lxml')
    heading = body.find('h1') or body.title
    title = page.title + ' ' + (heading.get_text(' ', strip=True) if heading else '')
    topic_terms = [topic] if topic else []
    if claim.category == '交流活动':
        topic_terms += re.findall(r'[“"]([^”"]{2,30})[”"]', topic)
    # Common project/award notice headings can abbreviate the full input title.
    topic_terms += [t for t in ('教学改革', '奖学金', '毕业设计', '学生会', '科协')
                    if t in claim.raw_text]
    project_page = False
    if claim.category == '项目':
        heading_text = _compact(heading.get_text(' ', strip=True)) if heading else ''
        page_title = _compact(page.title)
        project_terms = [*filter(None, _project_topics(claim)), *identifiers(claim)]
        project_page = any(_compact(term) in heading_text or _compact(term) in page_title
                           for term in project_terms)
        topic_terms += project_terms
    if not project_page and not any(t and t in title for t in topic_terms): return []
    year = re.search(r'(?:19|20)\d{2}', claim.date_start or claim.date_label or '')
    year = year.group() if year else ''
    result = []; seen = set()
    for a in body.select('a[href], [pdfsrc]'):
        url = urljoin(page.url, a.get('pdfsrc') or a.get('href', ''))
        label = a.get('title') or a.get_text(' ', strip=True)
        if urlparse(url).scheme not in ('http', 'https') or not url_in_domain(url, domain) or url == page.url or url in seen: continue
        document = bool(re.search(r'\.(?:pdf|docx?|xlsx?)(?:$|[?#])', url, re.I) or a.has_attr('pdfsrc'))
        years = set(re.findall(r'(?:19|20)\d{2}', label))
        if year and years and year not in years: continue
        member_link = project_page and bool(re.fullmatch(
            r'(?:(?:项目组|课题组|团队|实验室|课题|项目|现任|历届)?成员(?:名单|介绍|一览)?|人员名单|研究团队|团队介绍|(?:lab |team )?members)(?:\.(?:pdf|docx?|xlsx?))?',
            label.strip(), re.I))
        if not document and not member_link and not (
                any(t and t in label for t in topic_terms)
                and re.search(r'名单|公示|立项|结题|表彰|报道|任命|换届|新闻|参访', label)):
            continue
        if document and not member_link and not (
                a.has_attr('pdfsrc') or re.search(r'附件|名单|公示|立项|结题|表彰|汇总', label)):
            continue
        seen.add(url);result.append(SearchHit(url, title=title.strip() + ' · ' + (label or 'PDF附件'), publisher=page.publisher))
    return result
