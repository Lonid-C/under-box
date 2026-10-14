"""输入主页与现任机构渠道只提供背景；核验从独立第三方取证。"""
import re
from .homepage import same_page
from .plan import school_aliases
from .search import url_in_domain


def policy(urls=(), domains=(), organizations=()):
    values = {'urls': list(dict.fromkeys(urls)), 'domains': list(dict.fromkeys(domains)),
              'organizations': list(dict.fromkeys(organizations))}
    return values if any(values.values()) else []


def _parts(sources):
    return sources if isinstance(sources, dict) else {'urls': sources or [], 'domains': [], 'organizations': []}


def blocked_site(site, sources):
    if not site:
        return False
    url = site if '://' in site else 'https://' + site
    return any(url_in_domain(url, domain) for domain in _parts(sources).get('domains', []))


def blocked_query(query, site, sources):
    return blocked_site(site, sources) or any(blocked_site(s, sources) for s in
        re.findall(r'(?<![-\w])site:([^\s]+)', query, re.I))


def third_party_text(query, sources):
    """移除被排除渠道的正向限定，保留核对事实与身份的关键词。"""
    return re.sub(r'\s+', ' ', re.sub(r'(?<![-\w])site:([^\s]+)',
        lambda m: '' if blocked_site(m.group(1), sources) else m.group(), query, flags=re.I)).strip()


def blocked_publisher(publisher, sources):
    """核对发布主体，不能把报道正文提到任职单位当作该单位发布。"""
    if not isinstance(publisher, str) or not publisher:
        return False
    actual = re.sub(r'[\s（）()·\-—_]+', '', publisher).casefold()
    for org in _parts(sources).get('organizations', []):
        for alias in school_aliases(org):
            label = re.sub(r'[\s（）()·\-—_]+', '', alias).casefold()
            if not label:
                continue
            suffix = actual[len(label):] if actual.startswith(label) else ''
            if actual == label or suffix and (re.match(
                    r'^(?:官方|官网|公众号|新闻网|新闻中心|团委|学生|招生|教授|sz|music|official|news|school|college|faculty|institute|library)', suffix, re.I)
                    or re.fullmatch(r'.{0,30}(?:学院|学部|书院|研究院|研究所|中心|大学|图书馆|出版社|事业部|分公司)(?:官方.*)?', suffix)):
                return True
    return False


def excluded(url, sources, publisher=''):
    parts = _parts(sources)
    return blocked_publisher(publisher, sources) or isinstance(url, str) and (
        blocked_site(url, sources) or any(same_page(url, source) for source in parts.get('urls', [])
                                         if isinstance(source, str) and source))


def claim_sources(claim):
    ent = claim.entities or {}
    employer = _parts(ent.get('excluded_employer_sources', []))
    return policy([*ent.get('excluded_source_urls', []), *employer.get('urls', [])],
                  employer.get('domains', []), employer.get('organizations', []))


def for_claim(claim, sources):
    """本校就读/本校荣誉是用途限定的例外；不放行本校证明别校学历或其他经历。"""
    from .plan import claim_school, school_domain, _school_record
    from .identity import matches_school
    parts = _parts(sources)
    eligible = claim.category in {'学历', '校内荣誉', '奖学金'} or (
        claim.category == '竞赛' and bool(re.search(r'校内|校级|本校', claim.raw_text)))
    school = claim_school(claim)
    allowed = []
    if eligible and school:
        # 可按整份输入已建立的教育范围定位校内奖项，不能单靠模型猜出学校。
        context = (claim.entities or {}).get('candidate_identity_context') or {}
        in_input = matches_school(school, claim.raw_text) or claim.category in {'校内荣誉', '奖学金'} and any(
            matches_school(school, s) for s in context.get('education_schools', []))
        if in_input:
            def institution_key(value):
                record = _school_record(value)
                return re.sub(r'[\s（）()]+', '', record['name'] if record else value).casefold()
            allowed = [org for org in parts.get('organizations', []) if institution_key(school) == institution_key(org)]
    blocked_orgs = [org for org in parts.get('organizations', []) if org not in allowed]
    mapping = parts.get('domains_by_org', {})
    allowed_domains = {d for org in allowed for d in mapping.get(org, [school_domain(org)]) if d}
    still_blocked = {d for org in blocked_orgs for d in mapping.get(org, [school_domain(org)]) if d}
    domains = [d for d in parts.get('domains', []) if d not in allowed_domains or d in still_blocked]
    result = policy(parts.get('urls', []), domains, blocked_orgs)
    if allowed and not result:
        result = {'urls': [], 'domains': [], 'organizations': []}
    if isinstance(result, dict):
        result['allowed_academic_institutions'] = allowed
    return result


def evidence_excluded(evidence, sources):
    return excluded(evidence.url, sources, getattr(evidence, 'publisher', '')) or excluded(getattr(evidence, "origin_url", ""), sources)


class SourceGuardSearcher:
    def __init__(self, inner, sources):
        self.inner, self.sources = inner, sources

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _query(self, query, site):
        if blocked_query(query, site, self.sources):
            return None
        domains = _parts(self.sources).get('domains', [])
        return query + ''.join(' -site:' + d for d in domains if '-site:' + d not in query)

    def _hits(self, hits):
        return [h for h in hits if not excluded(h.url, self.sources, getattr(h, 'publisher', ''))]

    def search(self, query, site=None):
        query = self._query(query, site)
        return self._hits(self.inner.search(query, site=site)) if query is not None else []

    def search_with_confirmation(self, query, site=None, accept=None):
        query = self._query(query, site)
        if query is None:
            return []
        reader = getattr(self.inner, 'search_with_confirmation', None)
        if callable(reader):
            guarded_accept = lambda h: not excluded(h.url, self.sources, getattr(h, 'publisher', '')) and (accept(h) if accept else True)
            return self._hits(reader(query, site=site, accept=guarded_accept))
        return self._hits(self.inner.search(query, site=site))

    def confirm_empty(self, query, site=None):
        query = self._query(query, site)
        reader = getattr(self.inner, 'confirm_empty', None)
        return self._hits(reader(query, site=site)) if query is not None and callable(reader) else []


class SourceGuardFetcher:
    def __init__(self, inner, sources):
        self.inner, self.sources = inner, sources

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _read(self, url, reader, *args, **kwargs):
        if excluded(url, self.sources):
            return None
        body = reader(url, *args, **kwargs)
        resolved = getattr(self.inner, "resolved_urls", {}).get(url, url)
        return None if excluded(resolved, self.sources) else body

    def get(self, url, *args, **kwargs):
        return self._read(url, self.inner.get, *args, **kwargs)

    def get_roster(self, url, claim):
        reader = getattr(self.inner, "get_roster", None)
        return self._read(url, reader, claim) if callable(reader) else self.get(url)
