"""Bounded web search for report QA; results never mutate verification reports."""
from __future__ import annotations

import re

from .collect import Budget
from .parse import detect_injection, scrub
from .plan import claim_school, school_domain
from .internship import company_name
from .public_context import now, public_url, visible_text, read_safely, search_safely
from .search import build_searcher, PageFetcher


def question_query(question, report):
    """Resolve report references without sending the whole resume to a search provider."""
    question = scrub(question, detect_injection(question)).strip()
    claims = list(report.claims)
    ids = re.findall(r"\bc\d+\b", question, re.I)
    if ids:
        claims = [v for v in claims if v.claim.id.lower() in {s.lower() for s in ids}] or claims
    companies = list(dict.fromkeys(company_name(v.claim) for v in claims if v.claim.category in {"实习", "任职"}))
    schools = list(dict.fromkeys(claim_school(v.claim) for v in claims if v.claim.category == "学历"))
    context = []
    for names, pattern in ((companies, r"公司|企业|实习|经营|法人|主营"),
                           (schools, r"学校|大学|专业|院系|学院|教务|学历")):
        names = [n for n in names if n]
        if len(names) == 1 and re.search(pattern, question) and names[0] not in question:
            context.append(names[0])
    return re.sub(r"\bc\d+\b", "", " ".join([*context, question]), flags=re.I).strip()[:600]


def search_question(question, report, *, searcher=None, fetcher=None):
    searcher = searcher or build_searcher()
    fetcher = fetcher or PageFetcher(timeout=7, connect_timeout=4)
    budget = Budget(max_searches=1, max_page_reads=3, max_seconds=40)
    result = {"query": question_query(question, report), "sources": [], "limitations": [], "searchedAt": now()}
    if not result["query"]:
        result["limitations"].append("没有可用于公开检索的问题正文。")
        return result
    site = None
    if re.search(r"学校|大学|专业|院系|学院|教务|学历", question):
        schools = {claim_school(v.claim) for v in report.claims if v.claim.category == "学历"}
        matched = [school for school in schools if school and school in result["query"]]
        if len(matched) == 1:
            site = school_domain(matched[0])
    result["site"] = site
    hits = search_safely(searcher, result["query"], site, budget, result["limitations"])
    seen = set()
    for hit in hits:
        url = public_url(hit.url)
        if not url or url in seen:
            continue
        seen.add(url)
        html, final = read_safely(fetcher, url, budget)
        if html:
            from .parse import extract_main_text
            raw = extract_main_text(html)
            text = scrub(raw, detect_injection(raw))[:1800]
        else:
            text = ""
        if not text:
            text = scrub(hit.snippet or "", detect_injection(hit.snippet or ""))[:1200]
        if not text.strip():
            continue
        result["sources"].append({"id": "W" + str(len(result["sources"]) + 1),
            "title": scrub(hit.title or url, detect_injection(hit.title or url))[:180],
            "url": final if html else url, "text": text, "accessedAt": now(),
            "extractionMethod": "text" if html else "search_snippet"})
        if len(result["sources"]) >= 5:
            break
    if not result["sources"]:
        result["limitations"].append("本次联网检索未取得可引用资料；不能据此判断经历不实。")
    elif any(s["extractionMethod"] == "search_snippet" for s in result["sources"]):
        result["limitations"].append("部分来源只有搜索摘要，未回读原文，需打开链接核对。")
    return result
