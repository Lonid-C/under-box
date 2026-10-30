"""Public office contacts for unresolved claims. Never used as candidate evidence."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from .plan import claim_school, school_domain, school_aliases
from .competitions import competition_for_claim
from .public_context import (compact, now, public_url, visible_text, public_contacts,
                             search_safely, read_safely, EMAIL)
from .search import url_in_domain

DIRECTORY = Path(__file__).resolve().parents[1] / "data" / "contact_routes.json"
EXCLUDED = {"项目", "开源项目"}
CONTACT_HEADING = re.compile(r"联系|办公电话|教务|学籍|学生事务|人事|人力资源|Contact|Enquir", re.I)


def needs_contacts(vc):
    return vc.status != "ok" and vc.claim.category not in EXCLUDED


def valid_contact(contact):
    value = str(contact.get("value") or contact.get("phone") or contact.get("email") or "").strip()
    if not public_url(contact.get("sourceUrl")) or value not in str(contact.get("excerpt", "")):
        # Directory entries may normalize spacing/country codes in their phone values.
        raw_digits = re.sub(r"\D", "", str(contact.get("excerpt", "")))
        digits = re.sub(r"\D", "", value)
        if not (public_url(contact.get("sourceUrl")) and contact.get("type") == "phone"
                and len(digits) >= 8 and digits.lstrip("0") in raw_digits):
            return False
    if contact.get("type") == "email":
        return bool(EMAIL.fullmatch(value))
    return contact.get("type") == "phone" and bool(re.fullmatch(r"[+\d() -]{8,32}", value)) and 8 <= len(re.sub(r"\D", "", value)) <= 15


def directory_contacts(claim, institution=None):
    try:
        routes = json.loads(DIRECTORY.read_text(encoding="utf-8"))["routes"]
    except (OSError, ValueError, KeyError):
        return []
    domain = school_domain(claim_school(claim)) or ""
    result = []
    for route in routes:
        if claim.category not in route["categories"]:
            continue
        if route.get("schoolDomain") and route["schoolDomain"] != domain:
            continue
        if route.get("institutionNames") and compact(institution or claim_school(claim)) not in {compact(name) for name in route["institutionNames"]}:
            continue
        if route.get("pattern") and not re.search(route["pattern"], claim.raw_text, re.I):
            continue
        for item in route["contacts"]:
            contact = {**item, "kind": "公开办公渠道", "institution": route["institution"],
                       "scope": route["scope"], "sourceUrl": route["sourceUrl"],
                       "excerpt": route["excerpt"], "accessedAt": route["verifiedAt"],
                       "extractionMethod": "curated_official", "routeId": route["id"]}
            # A curated route is reviewed with its exact source and normalization.
            if item["type"] == "phone":
                contact["originalValue"] = route["excerpt"]
            result.append(contact)
    return result


def existing_contacts(review):
    if not isinstance(review, dict):
        return []
    result = []
    institution = review.get("company") or review.get("school") or "相关机构"
    for item in review.get("contacts") or []:
        if not isinstance(item, dict):
            continue
        contact = deepcopy(item)
        contact["value"] = str(contact.get("value") or contact.get("phone") or contact.get("email") or "").strip()
        contact.setdefault("type", "email" if contact.get("email") else "phone")
        contact.setdefault("institution", institution)
        contact.setdefault("scope", contact.get("kind") or "机构一般咨询，可询问材料核对的受理部门")
        if review.get("subjectNote"):
            contact["subjectNote"] = review["subjectNote"]
        if valid_contact(contact):
            result.append(contact)
    for related in (review.get("relatedCompanies") or [])[:4]:
        result.extend(existing_contacts({**related, "relatedCompanies": []}))
    return result


def official_targets(claim, school_domains=None):
    """Resolve institutions from the school/contest catalogue, never guess a domain."""
    school = claim_school(claim)
    result = []
    competition = competition_for_claim(claim)
    if competition:
        for domain in competition.get("official_domains", [])[:2]:
            if public_url("https://" + domain + "/"):
                result.append((competition["name"], domain, "赛事官方办公渠道，可询问记录核对途径"))
    if school and (claim.category in {"学历", "奖学金", "校内荣誉", "学生工作", "教学活动"} or competition):
        domain = school_domain(school)
        # Non-catalogue hints require confirmation by reading the school's home.
        if domain:
            result.append((school, domain, "学校一般办公渠道，可询问对应核验事项的受理部门"))
    return list(dict.fromkeys(result))


def discover_target(claim, searcher, fetcher, budget, limitations):
    """Require a verbatim institution and an owner-attributed home before discovery."""
    candidates = []
    for key in ("company", "employer", "publisher", "issuer", "organization", "conference", "venue", "org", "school", "university"):
        value = claim.entities.get(key)
        if isinstance(value, str) and 3 <= len(value.strip()) <= 120 and compact(value) in compact(claim.raw_text) and not re.search(r"[<>\n]|https?://", value):
            candidates.append(value.strip())
    for institution in list(dict.fromkeys(candidates))[:1]:
        hits = search_safely(searcher, institution + " 官方网站 联系我们", None, budget, limitations)
        for hit in hits[:2]:
            if not public_url(hit.url):
                continue
            # Never infer ownership from a third-party profile/contact listing.
            home = urlparse(hit.url)._replace(path="/", params="", query="", fragment="").geturl()
            html, final = read_safely(fetcher, home, budget)
            if not html or urlparse(final).hostname != urlparse(home).hostname:
                continue
            soup = BeautifulSoup(html, "html.parser")
            heading = " ".join(n.get_text(" ", strip=True) for n in soup.select("title,h1"))
            text = visible_text(html)
            owner_lines = [line for line in text.splitlines() if re.search(r"版权所有|Copyright|©", line, re.I)]
            owner = any(compact(institution) in compact(line) for line in owner_lines)
            if compact(institution) in compact(heading) and owner:
                return (institution, urlparse(final).hostname, "机构网站公开办公渠道，可询问对应材料的核对途径")
    return None


def lookup_contacts(target, searcher, fetcher, budget, limitations):
    institution, domain, scope = target
    result, visited, candidates = [], set(), []
    hits = search_safely(searcher, institution + " 官网 联系方式 Contact", domain, budget, limitations)
    candidates.extend((h.url, h.title) for h in hits if public_url(h.url) and url_in_domain(h.url, domain)
                      and CONTACT_HEADING.search(h.title))
    # A catalogue home can supply actual links even when search is unavailable.
    candidates.append(("https://" + domain + "/", institution))
    i = 0
    while i < len(candidates) and budget.can_read():
        url, title = candidates[i]
        i += 1
        if url in visited:
            continue
        visited.add(url)
        html, final = read_safely(fetcher, url, budget)
        if not html or not url_in_domain(final, domain):
            continue
        soup = BeautifulSoup(html, "html.parser")
        heading = " ".join(n.get_text(" ", strip=True) for n in soup.select("title,h1,h2"))
        if CONTACT_HEADING.search(heading):
            # Contact pages of an unrelated department are not a university's central office.
            remainder = heading
            for alias in school_aliases(institution):
                remainder = remainder.replace(alias, "")
            if not re.search(r"学院|学部|[\u3400-\u9fff]{2,10}系(?:办公室|官网|首页)", remainder):
                for c in public_contacts(visible_text(html), final, kind="机构官网公开办公渠道"):
                    c.update(institution=institution, scope=scope)
                    if valid_contact(c):
                        result.append(c)
        if result:
            break
        for a in soup.select("a[href]"):
            label = a.get_text(" ", strip=True)
            linked = urljoin(final, a.get("href", ""))
            if CONTACT_HEADING.search(label) and public_url(linked) and url_in_domain(linked, domain) and linked not in visited:
                candidates.insert(i, (linked, label))
                if len(candidates) >= 12:
                    break
    return result


def build_contact_review(vc, searcher=None, fetcher=None, budget=None, school_domains=None, lookup=None):
    if not needs_contacts(vc):
        return None
    claim = vc.claim
    review = {"kind": "verification_contacts", "checkedAt": now(), "state": "not_found",
              "contacts": [], "links": [], "pendingElements": list(vc.unproved), "limitations": []}
    contacts = directory_contacts(claim, (vc.manual_review or {}).get("company"))
    if not contacts:
        contacts += existing_contacts(vc.manual_review) + existing_contacts(vc.education_review)
    elif vc.manual_review:
        # A dedicated institution route comes before generic parent-office channels.
        for related in (vc.manual_review.get("relatedCompanies") or [])[:4]:
            contacts += existing_contacts({**related, "relatedCompanies": []})
    if not contacts and searcher is not None and fetcher is not None and budget is not None:
        targets = official_targets(claim, school_domains)
        if not targets:
            discovered = discover_target(claim, searcher, fetcher, budget, review["limitations"])
            if discovered:
                targets = [discovered]
        for target in targets:
            review["links"].append({"title": target[0] + "官网", "url": "https://" + target[1] + "/"})
            contacts += lookup(target) if lookup else lookup_contacts(target, searcher, fetcher, budget, review["limitations"])
            if contacts:
                break
    seen = set()
    for contact in contacts:
        key = (contact.get("institution"), contact["type"], re.sub(r"\s", "", contact["value"]).casefold())
        if key not in seen:
            seen.add(key)
            review["contacts"].append(contact)
    review["contacts"] = review["contacts"][:12]
    if review["contacts"]:
        review["state"] = "public_contact_found"
    else:
        review["limitations"] = ["暂未找到对应机构公开的电话或邮箱，请补充单位全称或官网联系页。"]
    return review


def attach_contact_reviews(verified, searcher, fetcher, budget_factory, school_domains=None, progress=lambda *_: None):
    from threading import Lock
    from .usage import UsageExecutor
    pending = [vc for vc in verified if needs_contacts(vc)]
    if not pending:
        return
    progress("[6/6] 补充待核验条目的公开电话与邮箱")
    cache, locks, guard = {}, {}, Lock()
    def cached_lookup(target):
        with guard:
            lock = locks.setdefault(target, Lock())
        with lock:
            if target not in cache:
                cache[target] = lookup_contacts(target, searcher, fetcher, budget_factory(), [])
            return deepcopy(cache[target])
    def add(vc):
        # Every non-project unresolved claim receives a contact result, including
        # an explicit empty result if the institution publishes no usable channel.
        try:
            vc.verification_contacts = build_contact_review(vc, searcher, fetcher, budget_factory(), school_domains, cached_lookup)
        except Exception:
            vc.verification_contacts = {"kind": "verification_contacts", "state": "not_found", "contacts": [],
                "links": [], "pendingElements": list(vc.unproved),
                "limitations": ["联系方式暂未取得，可从对应机构官网联系页继续核对。"]}
    with UsageExecutor(max_workers=min(3, len(pending))) as pool:
        list(pool.map(add, pending))
