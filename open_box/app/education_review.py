"""Official department/program links and public offices when education is incomplete."""
from __future__ import annotations

import re
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from .plan import claim_school, school_domain, _school_record, school_aliases
from .search import url_in_domain
from .public_context import (compact, now, public_url, visible_text, source_field,
                             public_contacts, search_safely, read_safely)


def study_field(claim):
    values = [e.partition("=")[2].strip() for e in claim.elements
              if e.partition("=")[0].strip() in {"专业", "院系", "学院", "研究方向"}]
    values += [claim.entities.get(k) for k in ("major", "field", "dept", "department")]
    return next((v.strip() for v in values if isinstance(v, str) and 2 <= len(v.strip()) <= 100
                 and compact(v) in compact(claim.raw_text) and not re.search(r"[<>\n]|https?://", v)), "")


def build_education_review(claim, searcher, fetcher, budget, school_domains=None, progress=lambda *_: None):
    school, field = claim_school(claim), study_field(claim)
    query = " ".join(x for x in (school, field, "官网 专业介绍 联系方式") if x)
    review = {"kind": "education", "school": school, "field": field, "checkedAt": now(),
              "state": "school_missing", "links": [], "contacts": [], "profile": {},
              "queryUrl": "https://www.bing.com/search?q=" + quote(query, safe="") if school else "",
              "note": "院校、专业介绍和办公联系方式仅供人工核验；官网存在该专业不能证明候选人就读或取得学位。联系人是公开办公渠道，不代表候选人的导师或证明人。",
              "checks": ["核对学校、学院/专业、学段及就读时间", "请候选人提供授权核验材料，再联系教务或学籍管理部门"],
              "limitations": []}
    if not school:
        review["limitations"].append("本条未明确学校，不能推测院校或联系人。")
        return review
    progress("补充对应学校/专业的官网与公开办公联系渠道")
    record = _school_record(school) or {}
    domain = school_domain(school)
    # Dynamically discovered domains remain hints until the school's home is read.
    if not domain:
        domain = (school_domains or {}).get(school)
        if not domain:
            from .collect import _discover_school_domain
            hits = search_safely(searcher, school + " 官方网站", None, budget, review["limitations"])
            domain = _discover_school_domain(school, hits, homepage=True)
        if domain:
            home = "https://www." + domain.removeprefix("www.") + "/"
            html, final = read_safely(fetcher, home, budget)
            soup = BeautifulSoup(html or "", "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            if not (html and url_in_domain(final, domain)
                    and any(compact(name) in compact(title) for name in school_aliases(school))):
                domain = None
    if not domain:
        review["state"] = "official_domain_unconfirmed"
        review["limitations"].append("未能可靠定位学校官方域名，查询入口仅供人工选择，不猜官网网址或联系方式。")
        return review
    home = record.get("url") or "https://www." + domain.removeprefix("www.") + "/"
    if not public_url(home) or not url_in_domain(home, domain):
        home = "https://" + domain + "/"
    review["links"].append({"kind": "school", "title": school + "官网", "url": home,
                            "basis": "学校目录" if record else "官网首页标题已核对"})
    review["state"] = "official_links_found"
    candidates = []
    department_names = []
    if field:
        from .discover import match_college
        pick = match_college(record.get("colleges") or [], field)
        if pick and public_url(pick[1]) and url_in_domain(pick[1], domain):
            department_names.append(pick[0])
            candidates.append((pick[1], pick[0], True))
            review["links"].append({"kind": "department", "title": pick[0] + "官网（目录匹配，需核对专业）", "url": pick[1]})
    queries = iter((f'{school} {field} 专业介绍', f'{school} {field} 联系方式'))
    visited, links = set(), {item["url"] for item in review["links"]}
    i = 0
    while budget.can_read():
        if i >= len(candidates):
            query_text = next(queries, None)
            if query_text is None:
                break
            hits = search_safely(searcher, query_text, domain, budget, review["limitations"])
            hits = [h for h in hits if public_url(h.url) and url_in_domain(h.url, domain)
                    and not re.search(r"招聘|招标|获奖|竞赛|讲座|新闻", h.title)]
            candidates.extend((h.url, h.title, False) for h in hits[:2])
            if i >= len(candidates):
                continue
        url, title, catalog_dept = candidates[i]
        i += 1
        if url in visited:
            continue
        visited.add(url)
        html, final = read_safely(fetcher, url, budget)
        if not html or not url_in_domain(final, domain):
            continue
        soup = BeautifulSoup(html, "html.parser")
        heading = " ".join(n.get_text(" ", strip=True) for n in soup.select("title,h1,h2,h3"))
        text = visible_text(html)
        field_subject = bool(field and compact(field) in compact(heading))
        descriptive = next((visible_text(str(node)) for node in soup.select("p,.v_news_content,.wp_articlecontent")
            if field and compact(field) in compact(visible_text(str(node)))
            and 30 <= len(visible_text(str(node))) <= 1500
            and re.search(r"培养|课程|学科|专业|成立|研究", visible_text(str(node)))), "")
        department_subject = bool(descriptive and re.search(r"介绍|概览|概况|专业|学科|培养", heading))
        scoped = field_subject or department_subject or any(compact(name) in compact(heading) for name in department_names)
        if catalog_dept:
            scoped = compact(title) in compact(heading) or compact(title) in compact(text)
        contact_page = bool(re.search(r"联系|教务|学籍|学位|招生", heading))
        # A different department's contact page is not a university-wide office.
        if not scoped and re.search(r"学院|学部|[\u3400-\u9fff]{2,12}系(?:办公室|官网|首页|$)", heading.replace(school, "")):
            continue
        program_page = bool((field_subject or (descriptive and field + "专业" in descriptive)) and re.search(r"专业|培养|课程|学科|介绍|概况", heading))
        if not scoped and not contact_page:
            continue
        if final not in links:
            review["links"].append({"kind": "program" if program_page else "contact" if contact_page else "department",
                                    "title": (heading or title)[:180], "url": final,
                                    "scope": "对应专业/院系" if scoped else "学校办公渠道（未确认对应院系）"})
            links.add(final)
        if (program_page or department_subject) and "introduction" not in review["profile"]:
            for node in soup.select("p,article,.v_news_content,.wp_articlecontent"):
                paragraph = visible_text(str(node))
                if 30 <= len(paragraph) <= 1000 and compact(field) in compact(paragraph) and re.search(r"培养|课程|研究|学制|专业", paragraph):
                    review["profile"]["introduction"] = source_field("专业/院系介绍（官网原文节选，需核对专业口径）", paragraph[:500], paragraph[:600], final, heading)
                    break
        # University-wide offices are explicitly labeled, never claimed to be the program's staff.
        if scoped or contact_page:
            from .public_context import labeled_fields
            named = labeled_fields(text, {"contactPerson": ("联系人", "教务联系人")}, final, heading)
            if named:
                review["profile"].update(named)
            review["contacts"].extend(public_contacts(text, final,
                kind="院系公开办公联系渠道" if scoped else "学校公开办公联系渠道（未确认对应院系）"))
        # Follow only actual links from a confirmed department page, within the school domain.
        if scoped:
            for a in soup.select("a[href]"):
                label = a.get_text(" ", strip=True)
                target = urljoin(final, a.get("href", ""))
                if re.search(r"联系我们|联系方式|教务办公室|专业介绍|培养方案|机构设置|管理服务|办公电话|综合办公室", label) and public_url(target) and url_in_domain(target, domain):
                    candidates.insert(i, (target, label, True))
                    break
    seen = set()
    review["contacts"] = [c for c in review["contacts"]
                          if not ((c["type"], c["value"], c["sourceUrl"]) in seen
                                  or seen.add((c["type"], c["value"], c["sourceUrl"])))][:6]
    if review["contacts"]:
        review["state"] = "public_contact_found"
    else:
        review["limitations"].append("未取得已回读官网中的公开办公电话或邮箱；请从官网联系页人工确认，不推测负责人姓名和号码。")
    if not any(item["kind"] == "program" for item in review["links"]):
        review["limitations"].append("未定位到可确认的对应专业介绍页；学校/学院入口不等同于专业已核实。")
    return review
