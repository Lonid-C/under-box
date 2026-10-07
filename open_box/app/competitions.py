"""可维护的赛事目录、官网路由与公开名单范围；目录不是个人获奖证据。"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

DATA = Path(__file__).resolve().parent.parent / "data" / "competitions.json"
STATUSES = frozenset({"unknown", "public_names", "public_teams", "public_results",
                      "login_required", "not_public", "not_found"})
_cache: tuple[int, dict] | None = None


def load_catalog(path: str | Path | None = None) -> dict:
    global _cache
    p = Path(path) if path else DATA
    stamp = p.stat().st_mtime_ns
    if path is None and _cache and _cache[0] == stamp:
        return _cache[1]
    doc = json.loads(p.read_text(encoding="utf-8"))
    if doc.get("schema_version") != 1 or not isinstance(doc.get("competitions"), list):
        raise ValueError("赛事目录格式不正确")
    seen = set()
    for item in doc["competitions"]:
        ident = item.get("id")
        if not ident or ident in seen or not item.get("name") or not item.get("subjects"):
            raise ValueError(f"赛事目录缺字段或 id 重复：{ident}")
        seen.add(ident)
        pub = item.get("publication") or {}
        if pub.get("status") not in STATUSES:
            raise ValueError(f"赛事 {ident} 的名单状态不正确")
        # 不能因为一次搜索没有结果，就在维护表里写“不公开”。必须有主办方说明。
        if pub["status"] == "not_public" and not (pub.get("source_url") and pub.get("policy_quote")):
            raise ValueError(f"赛事 {ident} 缺少“不公开名单”的官方声明")
        if pub["status"] == "not_public" and not allowed_official_url(item, pub["source_url"]):
            raise ValueError(f"赛事 {ident} 的“不公开名单”声明不在已确认官方域")
        for url in [*item.get("official_urls", []),
                    *(x.get("url", "") for x in item.get("result_pages", []))]:
            parsed = urlparse(url)
            if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username:
                raise ValueError(f"赛事 {ident} 的公开网址不正确")
        for domain in [*item.get("official_domains", []), *item.get("authority_domains", [])]:
            if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain) or ".." in domain:
                raise ValueError(f"赛事 {ident} 的域名不正确")
    if path is None:
        _cache = (stamp, doc)
    return doc


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"[\s·•‘’“”\"'（）()、，,:：—–_-]+", "", value)


def _alias_matches(alias: str, text: str) -> bool:
    # 英文缩写按原文词界匹配，IMC 不能命中 IMMC，ICPC 不能命中 XICPC。
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9&/ .-]*", alias):
        pattern = re.escape(unicodedata.normalize("NFKC", alias).casefold())
        pattern = pattern.replace(r"\ ", r"\s+")
        source = unicodedata.normalize("NFKC", text).casefold()
        return bool(re.search(r"(?<![a-z0-9])" + pattern + r"(?![a-z0-9])", source))
    return _normalize(alias) in _normalize(text)


def find_competition(text: str, *, catalog: dict | None = None) -> dict | None:
    """最长、最具体的赛事名优先；只写“挑战杯”时不猜两个不同赛事。"""
    scored = []
    for item in (catalog or load_catalog())["competitions"]:
        aliases = [item["name"], *item.get("aliases", [])]
        score = max((len(_normalize(a)) for a in aliases if a and _alias_matches(a, text)), default=0)
        # “高教社杯”是冠名，多个赛事共用。仅有冠名时保持歧义，有学科上下文再路由。
        if score == len("高教社杯") and any(_alias_matches(c, text) for c in item.get("brand_context", [])):
            score += max(len(_normalize(c)) for c in item["brand_context"] if _alias_matches(c, text))
        if score:
            scored.append((score, item))
    if not scored:
        return None
    best = max(score for score, _ in scored)
    winners = [item for score, item in scored if score == best]
    return winners[0] if len(winners) == 1 else None


def competition_for_claim(claim) -> dict | None:
    if claim.category != "竞赛":
        return None
    entities = claim.entities or {}
    labels = [entities.get("contest"), *[e.partition("=")[2] for e in claim.elements
               if e.partition("=")[0].strip() in ("赛事", "比赛", "竞赛")]]
    if any(_normalize(str(v or "")) == "高教社杯" for v in labels):
        return find_competition(" ".join([str(v or "") for v in labels] + [claim.raw_text or ""]))
    for label in labels:
        if isinstance(label, str) and label.strip():
            match = find_competition(label)
            if match:
                return match
    return find_competition(claim.raw_text or "")


def competition_resolution_note(claim) -> str:
    if (claim.category == "竞赛" and "高教社杯" in str(claim.raw_text) + str(claim.entities)
            and not competition_for_claim(claim)):
        return "“高教社杯”是多个赛事共用的冠名；当前未识别出具体赛事，请补充数学建模、先进成图等完整比赛名称，避免查错官网。"
    return ""


def competition_site(contest: str) -> str | None:
    item = find_competition(contest)
    domains = (item or {}).get("official_domains") or []
    return domains[0] if domains else None


def claim_year(claim) -> int | None:
    for value in [(claim.entities or {}).get("year"), claim.date_start, claim.date_label, *(e.partition("=")[2] for e in claim.elements
                   if e.partition("=")[0].strip() in ("年份", "竞赛年份", "获奖年份"))]:
        match = re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", str(value or ""))
        if match:
            return int(match.group())
    return None


def _in_domains(url: str, domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return any(host == d or host.endswith("." + d) for d in domains)


def allowed_official_url(item: dict, url: str) -> bool:
    parsed = urlparse(url)
    return (parsed.scheme in ("https", "http") and not parsed.username
            and not _in_domains(url, item.get("excluded_domains", []))
            and _in_domains(url, [*item.get("official_domains", []), *item.get("authority_domains", [])]))


def seed_pages(item: dict, claim, *, limit: int = 2) -> list[dict]:
    year = claim_year(claim)
    level = competition_level(str(claim.raw_text) + str(claim.entities))
    pages = [p for p in item.get("result_pages", [])
             if allowed_official_url(item, p["url"])
             and not p.get("requires_login")
             and not p.get("inactive")
             and not (level == "provincial" and p.get("level") == "national")
             and not (level == "national" and p.get("level") == "provincial")
             and not competition_page_mismatch(claim, p.get("title", ""))
             and (not p.get("province") or p["province"] == claim_province(claim))
             and ((p.get("year") is None and p.get("kind") == "index")
                  or year is not None and p.get("year") == year)]
    # 对应年度的直接名单优先；没写年份时只访问通用索引，不把最近一年当作本人年份。
    pages.sort(key=lambda p: (p.get("year") is None, p.get("kind") != "roster"))
    specific = [p for p in pages if p.get("year") == year and p.get("kind") == "roster"] if year else []
    return (specific or pages)[:limit]


_PROVINCES = "内蒙古 黑龙江 北京 天津 河北 山西 辽宁 吉林 上海 江苏 浙江 安徽 福建 江西 山东 河南 湖北 湖南 广东 广西 海南 重庆 四川 贵州 云南 西藏 陕西 甘肃 青海 宁夏 新疆 香港 澳门 台湾".split()


def claim_province(claim) -> str:
    """只提取简历明确写出的省份，不把学校搜索线索变成参赛省份。"""
    entities = claim.entities or {}
    explicit = " ".join(str(entities.get(k) or "") for k in ("province", "region", "level", "track"))
    blob = str(claim.raw_text or "") + " " + explicit + " " + " ".join(
        e.partition("=")[2] for e in claim.elements if e.partition("=")[0] in ("省份", "赛区", "级别"))
    found = [p for p in _PROVINCES if p in explicit or re.search(re.escape(p) + r"(?:省|赛区|分赛|省赛)", blob)]
    return found[0] if len(found) == 1 else ""


def competition_page_mismatch(claim, title: str) -> str:
    """不同赛段/身份的名单不能核实当前奖项，也不能用来判定奖项不实。"""
    context = str(claim.raw_text or "") + str(claim.entities or {}) + str(claim.elements)
    practice = r"模拟赛|练习赛|体验赛|\b(?:mock|practice)\b"
    teacher_track = r"青年教师|教师赛道|教师组|教师英语能力|微课.*(?:赛|奖)"
    teacher_award = r"指导教师奖|优秀指导教师|教师获奖|教师名单"
    student_result = r"学生(?:获奖|名单|组|与|及)|参赛学生|队员|队伍|团队|作品"
    selection = r"校内选拔|校选拔|校级选拔|校赛|选拔赛"
    if re.search(practice, title, re.I) and not re.search(practice, context, re.I):
        return "模拟/练习赛名单与所述正式比赛阶段不符"
    teacher_only = (re.search(teacher_track, title) or
                    re.search(teacher_award, title) and not re.search(student_result, title))
    if teacher_only and not re.search(teacher_track + "|" + teacher_award, context):
        return "教师奖项名单与学生奖项身份不符"
    if re.search(selection, title) and not re.search(selection, context):
        return "校内选拔赛名单与所述赛事阶段不符"
    target_level, page_level = competition_level(context), competition_level(title)
    if target_level and page_level and target_level != page_level:
        return "省赛与全国决赛名单级别不符"
    return ""


def competition_level(text: str) -> str:
    if re.search(r"全国(?:总?决赛|[特一二三]等奖)|\bnational\s+(?:first|second|third|grand)\s+(?:prize|award)", text, re.I):
        return "national"
    if re.search(r"省赛|省级|省[特一二三]等奖|\bprovincial\b", text, re.I):
        return "provincial"
    if re.search(r"国赛|国家级", text):
        return "national"
    return ""


def useful_roster_hit(claim, hit) -> bool:
    """有返回值但全是教师赛/错误年份/首页，也视作关键名单查询没有召回。"""
    if competition_page_mismatch(claim, hit.title or ""):
        return False
    years = {int(y) for y in re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", hit.title or "")}
    year = claim_year(claim)
    if year and years and year not in years:
        return False
    if _RESULT_WORDS.search(hit.title or ""):
        return True
    item = competition_for_claim(claim)
    return bool(item and allowed_official_url(item, hit.url)
                and any(not p.get("requires_identity")
                        and urlparse(hit.url).path.rstrip("/") == urlparse(p["url"]).path.rstrip("/")
                        for p in seed_pages(item, claim)))


def catalog_query_specs(item: dict, claim, name: str) -> list[dict]:
    domains = list(item.get("official_domains", []))
    for page in item.get("result_pages", []):
        host = (urlparse(page["url"]).hostname or "").removeprefix("www.")
        if host and allowed_official_url(item, page["url"]) and host not in domains:
            domains.append(host)
    domains = domains[:2]
    if not domains:
        return []
    label = item.get("search_label") or item["name"]
    if len(label) > 38:
        label = next((a for a in item.get("aliases", []) if 3 <= len(a) <= 32), label[:38])
    year = str(claim_year(claim) or "")
    terms = "获奖名单" if item.get("region") == "China" else "results winners"
    label = label if item.get("quote_search_label") is False else f'"{label}"'
    province = claim_province(claim)
    # 不强制匹配带“全国/大学生”的完整标题：各官网公告常省略这些词。
    scope = (province + " 省赛" if province and item.get("regional_query")
             and competition_level(str(claim.raw_text) + str(claim.entities)) == "provincial" else "")
    out = []
    for index, domain in enumerate(domains):
        out.append(dict(text=" ".join(f'{label} {year} {scope} {terms}'.split()), site=domain,
                        source=f'organizer:竞赛:catalog:{item["id"]}',
                        purpose="在预设官网按年度查整批赛果，再从名单核对本人", round=1,
                        weight=100-index, expect_tier="A", confirm_empty=index == 0))
    entities = claim.entities or {}
    anchor_keys = item.get("result_anchor_keys") or ("team_id", "team_number", "team", "project", "title")
    anchor = next((str(entities[k]).strip() for k in anchor_keys
                   if isinstance(entities.get(k), (str, int)) and str(entities[k]).strip()),
                  "" if item.get("result_anchor_keys") else name)
    if anchor:
        out.append(dict(text=" ".join(f'"{anchor}" {year} {terms}'.split()), site=domains[0],
                        source=f'organizer:竞赛:catalog:{item["id"]}',
                        purpose="按队号、队名、作品或姓名核对官方赛果", round=2,
                        weight=95, expect_tier="A"))
    from .plan import claim_school, school_domain
    school = claim_school(claim) or entities.get("school_search_hint", "")
    school_host = school_domain(school)
    if school_host:
        school_label = label if item.get("search_label") else f'"{item["name"].split("（", 1)[0]}"'
        out.append(dict(text=" ".join(f'{school_label} {year} {scope} 获奖名单'.split()),site=school_host,
                        source="school:竞赛:catalog-roster",purpose="补查学校官网整批名单及内嵌附件，再核对本人",
                        round=1,weight=120,expect_tier="A",confirm_empty=True))
    return out


_RESULT_WORDS = re.compile(r"获奖|授奖|公示|赛果|成绩|结果|名单|\b(?:results?|winners?|awards?|standings|rankings?|scoreboard)\b", re.I)


def roster_links(html: str, page, claim, *, limit: int | None = None,
                 school_domains: dict[str, str] | None = None) -> list[dict]:
    """官网索引→年度公告→PDF/XLSX名单，共用读页预算，拒绝外站与错误年份。"""
    item = competition_for_claim(claim)
    if not item or "<" not in html[:2000]:
        return []
    from .plan import claim_school, school_domain
    school = claim_school(claim) or (claim.entities or {}).get("school_search_hint", "")
    school_host = school_domain(school) or (school_domains or {}).get(school)
    school_page = bool(school_host and _in_domains(page.url, [school_host]))
    if not allowed_official_url(item, page.url) and not school_page:
        return []
    def allowed(url):
        # 学校补查只跟进已确认的该校官网，不能据简历给出的任意域名扩大范围。
        return allowed_official_url(item, url) or (school_page and
                urlparse(url).scheme in ("http", "https") and not urlparse(url).username
                and _in_domains(url, [school_host]))
    limit = max(1, min(6, int(limit if limit is not None else item.get("result_link_limit", 2))))
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    year = claim_year(claim)
    page_is_result = bool(_RESULT_WORDS.search(page.title or ""))
    choices = []
    seen = set()
    track_terms = [str((claim.entities or {}).get(k) or "").strip() for k in ("track", "division", "level")]
    track_terms += [e.partition("=")[2] for e in claim.elements
                    if e.partition("=")[0].strip() in ("赛道", "组别", "赛区", "级别")]
    track_terms = [term for term in track_terms if len(term) >= 2]
    nodes = list(soup.select("a[href], [pdfsrc]"))
    # VSB 公告常把附件放在函数的字面量参数里。只读取网址，不执行网页 JavaScript。
    for script in soup.find_all("script"):
        for match in re.finditer(r'''showVsbpdfIframe\(\s*["']([^"']+)["']''', script.get_text()):
            container = script.find_parent(["p", "div"])
            previous = container.find_previous(["p", "h2", "h3", "h4"]) if container else None
            label = previous.get_text(" ", strip=True) if previous else ""
            node = soup.new_tag("a", href=match.group(1))
            node.string = label[:220] or (page.title + " 内嵌PDF附件")
            nodes.append(node)
    for node in nodes:
        url = urljoin(page.url, node.get("pdfsrc") or node.get("href", ""))
        if (url in seen or not allowed(url)
                or url.split("#", 1)[0].rstrip("/") == page.url.split("#", 1)[0].rstrip("/")):
            continue
        seen.add(url)
        label = node.get("title") or node.get_text(" ", strip=True)
        if competition_page_mismatch(claim, label):
            continue
        province = claim_province(claim)
        # 省赛索引先取对应省；广东陈述不能用福建名单占掉有限的跟进预算。
        if province and "赛区" in label and any(p in label and p != province for p in _PROVINCES):
            continue
        years = {int(v) for v in re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", label)}
        if years and (year is None or year not in years):
            continue
        if item["id"] == "ncda" and any(t in label for t in ("教创", "教师")):
            continue
        if (re.search(r"教师|组织奖|优秀组织", label)
                and not re.search(r"学生|作品|团队|队员", label)
                and not re.search(r"指导教师奖|教师奖|组织奖", claim.raw_text)):
            continue
        document = bool(re.search(r"\.(?:pdf|docx?|xlsx?)(?:$|[?#&\s])", unquote(url), re.I))
        if (item["id"] == "mcm-icm" and document and not school_page
                and not any((claim.entities or {}).get(k) for k in ("team_id", "team_number"))):
            # 主办方完整名单按队号列结果，缺队号时读六个题组会先耗光学校补查预算。
            # 先拿年度结果入口和学校成员名单，拿到队号后再查对应题组。
            continue
        relevant = bool(_RESULT_WORDS.search(label))
        # COMAP “2023 → Results”这样的索引中，年份在同一列表项/表格行或 URL 中。
        context = node.find_parent(["tr", "li"])
        local = context.get_text(" ", strip=True) if context else ""
        local_years = {int(v) for v in re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", local)} if not years else set()
        url_years = {int(v) for v in re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", unquote(urlparse(url).path))}
        if any(found and (year is None or year not in found) for found in (local_years, url_years)):
            continue
        if not relevant and not (document and page_is_result
                                  and (node.has_attr("pdfsrc") or any(x in label for x in ("附件", "下载", "PDF")))):
            continue
        if re.search(r"报名|参赛须知|竞赛通知|报名名单|晋级|指导教师|组织奖|优秀组织", label) and not re.search(r"学生|作品|获奖名单", label):
            continue
        navigation = bool(re.search(r"\bmatrix\b|\bprevious\b|\ball\b.*\b(?:results|problems|contests)\b|历届|历年|往届", label, re.I))
        dated = bool(year and (year in years or year in url_years or local_years == {year} and not navigation))
        score = (4*document + 12*bool(year and year in (years | url_years))
                 + 3*bool(year and local_years == {year} and not navigation)
                 + 10*sum(term in label for term in track_terms) + int(relevant))
        if item["id"] == "mcm-icm" and document:
            # 按题目的完整赛果比 COMAP 奖学金新闻更适合核对普通美赛获奖。
            if re.search(r"\b(?:complete|full)\b.*\bresults?\b", label, re.I):
                score += 20
            raw = claim.raw_text or ""
            if re.search(r"\bICM\b", raw, re.I) and not re.search(r"\bMCM\b", raw, re.I) and re.search(r"\bMCM\b", label, re.I):
                continue
            if re.search(r"\bMCM\b", raw, re.I) and not re.search(r"\bICM\b", raw, re.I) and re.search(r"\bICM\b", label, re.I):
                continue
        choices.append((score, dict(url=url, title=label or f'{item["name"]} 官方获奖名单附件'), dated, document))
    if any(dated for _, _, dated, _ in choices):
        choices = [c for c in choices if c[2] or c[3]]
    choices.sort(key=lambda p: -p[0])
    return [value for _, value, _, _ in choices[:limit]]


def lookup_summary(item: dict) -> dict:
    pub = item["publication"]
    return dict(id=item["id"],name=item["name"],subjects=item["subjects"],
                official_urls=item.get("official_urls", []),publication_status=pub["status"],
                checked_at=pub.get("checked_at"),scope_note=pub.get("scope_note", ""),
                publication_source_url=pub.get("source_url"),policy_quote=pub.get("policy_quote"),
                result_urls=[p["url"] for p in item.get("result_pages", [])])


def publication_note(item: dict) -> str:
    pub = item["publication"]
    status = pub["status"]
    lead = {
        "unknown": "尚未核实官网公开获奖名单入口，不代表官网不提供名单。",
        "not_found": "目录核查暂未找到官网公开获奖名单入口，不代表该奖项不存在。",
        "not_public": "主办方官方说明不公开获奖名单，需候选人补充证明。",
        "login_required": "官网名单/个人结果需要登录，当前不能从公开页面完成核验。",
        "public_names": "官网已发现公开获奖名单，仍须核对简历对应年份、赛道、组别和本人条目。",
        "public_teams": "官网公开团队/学校/作品赛果；团队获奖不能单独证明本人属于该队。",
        "public_results": "官网已发现获奖公告或查询入口，需继续核对具体年度及附件。",
    }[status]
    note = f'{item["name"]}：{lead} {pub.get("scope_note", "")}'.strip()
    if status == "not_public":
        note += f' 官方说明：{pub["policy_quote"]}（{pub["source_url"]}）'
    return note
