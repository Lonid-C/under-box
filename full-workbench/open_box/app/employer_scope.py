"""从整份输入明示的现任经历确定需要排除的机构；不把过去经历或就读算作供职。"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .identity import matches_school
from .plan import _load_schools, _school_record, school_domain
from .search import url_in_domain
from .source_guard import policy

CURRENT = r"现任|目前(?:任职于|供职于|就职于|在)?|当前(?:任职于|供职于|就职于|在)?|现在(?:任职于|供职于|就职于|在)?|任职于|供职于|就职于|受聘于|currently\s+(?:works?\s+(?:at|for)|employed\s+(?:at|by)|at)|works?\s+(?:at|for)"
JOB = r"教授|讲师|研究员|教师|导师|职员|员工|工程师|经理|总监|院长|主任|顾问|任职|供职|就职|受聘|\b(?:professor|lecturer|researcher|engineer|employee|director|works?)\b"
PAST = r"曾任|曾在|原任|原为|离职|已离开|退休|曾(?:经)?$|过去$|formerly|previously|retired"


def _canonical(org):
    r = _school_record(org)
    return r['name'] if r else org.strip()


def _current_parts(text, name='', own=False):
    for match in re.finditer(CURRENT, text or '', re.I):
        before = re.split(r'[。；;\n]', text[:match.start()])[-1][-80:]
        # 同一段里“导师张某现任…”不是候选人的任职；曾在/原任也不是现任。
        if re.search(PAST, before, re.I) and not re.match(r'现任|目前|当前|现在|currently', match.group(), re.I):
            continue
        if not own and name and name not in before:
            if before.strip(' ,，:：') and not re.fullmatch(r'(?:他|她|本人|工作单位|任职单位|当前单位|现任单位)[ ,，:：]*', before):
                continue
        tail = re.split(r'[，,。；;\n]', text[match.end():], maxsplit=1)[0][:160].strip(' ：:')
        is_job = re.search(JOB, match.group() + ' ' + tail, re.I) or re.search(r'现任|任职|供职|就职|受聘|employed', match.group(), re.I)
        if tail and is_job:
            yield match.group() + ' ' + tail, tail


def current_employers(claims, text='', candidate_name=''):
    """模型拆漏现任条目时仍读输入原文；实体猜测必须在原文任职段里得到确认。"""
    orgs = []
    parts = list(_current_parts(text, candidate_name))
    for claim in claims:
        if claim.category != '任职':
            continue
        raw = claim.raw_text or ''
        own_parts = list(_current_parts(raw, own=True))
        parts.extend(own_parts)
        ent = claim.entities or {}
        fields = [ent.get('company'), ent.get('org'), ent.get('school')]
        fields += [e.partition('=')[2].strip() for e in claim.elements
                   if e.partition('=')[0].strip() in {'单位','公司','学校','机构','任职单位','任职组织'}]
        dated_current = bool(re.search(r'至今|在职|present|current', claim.date_label or '', re.I))
        for org in fields:
            if not isinstance(org, str) or not org or not matches_school(org, raw):
                continue
            if any(matches_school(org, part) for part, _ in own_parts) or (dated_current and not re.search(PAST, raw, re.I)):
                orgs.append(_canonical(org))
    for part, tail in parts:
        schools = [s for s in _load_schools() if any(a and a in tail for a in [s['name'], *s.get('aliases', [])])]
        # 学校全称可能是另一学校名称的前缀，优先最长的明示名称。
        if schools:
            best = max(schools, key=lambda s: max(len(a) for a in [s['name'], *s.get('aliases', [])] if a and a in tail))
            orgs.append(best['name'])
        else:
            org = re.match(r'(.{2,100}?(?:有限责任公司|股份有限公司|有限公司|公司|大学|学院|研究院|研究所|事务所|委员会|中心|局))', tail)
            if org:
                orgs.append(_canonical(org.group(1)))
    return list(dict.fromkeys(orgs))


def employer_policy(claims, doc, name, searcher, say=lambda *_: None):
    """已知学校直接映射；未知单位只发现单位官网，不用候选人姓名发现官网。"""
    urls = (getattr(doc, 'source_urls', None) or [doc.source]) if getattr(doc, 'kind', '') == 'web' else []
    employers = current_employers(claims, getattr(doc, 'safe_text', ''), name)
    schools = []
    from .plan import claim_school
    for c in claims:
        school = claim_school(c)
        if c.category == '学历' and school and matches_school(school, c.raw_text) and re.search(
                r'至今|在读|目前|当前|present|currently', c.raw_text + ' ' + c.date_label, re.I):
            schools.append(_canonical(school))
    orgs = list(dict.fromkeys([*employers, *schools]))
    domains = []
    mapping = {}
    for org in orgs:
        record = _school_record(org)
        known = [school_domain(org), *((record or {}).get('domain_aliases') or [])]
        mapping[org] = [d for d in known if d]
        domains.extend(d for d in known if d)
        if any(known):
            continue
        # 未收录学校/公司的官网发现与取证分开，不把发现结果当作证明。
        from .collect import _discover_official_domain, _discover_school_domain, _base_domain
        hits = searcher.search(f'"{org}" 官方网站')
        domain = _discover_school_domain(org, hits, homepage=True) or _discover_official_domain(org, hits)
        if domain:
            domains.append(_base_domain(domain))
            mapping[org].append(domains[-1])
            say(f'现任机构官网排除范围：{org} → {domains[-1]}')
        else:
            say(f'未确认现任机构 {org} 的唯一官网；仍排除能识别发布主体的该机构材料')
    # 同一机构的旧域/独立域，以输入原文中的官方主页地址补充，不猜同集团/母校归属。
    for url in urls:
        host = (urlparse(url).hostname or '').lower()
        if any(url_in_domain(url, d) for d in domains):
            continue
        record = next((s for s in _load_schools() if s['name'] in orgs and
                       any(url_in_domain(url, d) for d in [s.get('domain'), *s.get('domain_aliases', [])] if d)), None)
        if record and host:
            domains.append(host)
    result = policy(urls, domains, orgs)
    if isinstance(result, dict):
        result.update(domains_by_org=mapping, current_employers=employers)
    return result
