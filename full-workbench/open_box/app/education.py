"""搜索索引缺少教师页时，从已知学校院系目录读取导师培养记录。"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from .discover import match_college, _core
from .plan import _school_record, claim_school, school_name
from .search import SearchHit, url_in_domain


def faculty_training_pages(claim, candidate_name: str, domain: str, fetch, *, max_reads=6, select=None) -> list[SearchHit]:
    """只跟本校真实目录链接，至多六页；返回实际正文，交给统一身份/证据过滤。"""
    from bs4 import BeautifulSoup
    school = claim_school(claim)
    context = (claim.entities or {}).get("candidate_identity_context") or {}
    fields = [item.get("field", "") for item in context.get("education_fields", [])
              if isinstance(item, dict) and school_name(item.get("school")) == school_name(school)]
    fields += [e.partition("=")[2].strip() for e in claim.elements if e.partition("=")[0] in {"专业", "院系", "学院"}]
    record = _school_record(school) or {}
    configured = [item for item in record.get("faculty_directories", [])
                  if any(field == term or term in field for field in fields for term in item.get("fields", []))
                  and url_in_domain(item.get("url", ""), domain)]
    colleges = record.get("colleges") or []
    picks = [pick for hint in fields if (pick := match_college(colleges, hint))]
    # 专业不足以唯一定位时不遍历整所大学，也不猜院系地址。
    if not configured and len({p[1] for p in picks}) != 1:
        return []
    home = configured[0]["url"] if configured else picks[0][1]
    if not url_in_domain(home, domain):
        return []
    used, visited = 0, set()

    def get(url):
        nonlocal used
        if used >= max_reads or url in visited or not url_in_domain(url, domain):
            return None
        used += 1
        visited.add(url)
        return fetch(url)

    html = get(home)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    directories = []
    for a in soup.select("a[href]"):
        label = a.get_text(" ", strip=True)
        url = urljoin(home, a.get("href", ""))
        if not url_in_domain(url, domain):
            continue
        exact_field = any(len(core := _core(field)) >= 2 and core in _core(label) for field in fields)
        faculty_label = any(term in label for term in ("师资力量", "师资队伍", "教师名录", "导师名录"))
        if not (exact_field and len(label) <= 20 or faculty_label):
            continue
        parent, nearby = a, ""
        for _ in range(3):
            parent = parent.parent
            if parent is None:
                break
            nearby += parent.get_text(" ", strip=True)[:600]
        rank = 10 * exact_field + 5 * ("师资" in nearby or "教师" in nearby)
        directories.append((rank, url))
    pages, profile_urls = [], set()
    entries = [(100, home)] if configured else sorted(dict.fromkeys(directories), reverse=True)[:2]
    for _, directory in entries:
        listing = html if directory == home else get(directory)
        if not listing:
            continue
        parsed = BeautifulSoup(listing, "lxml")
        profiles = []
        for a in parsed.select("a[href]"):
            heading = a.find(["h2", "h3", "h4"])
            name = heading.get_text(" ", strip=True) if heading else a.get_text(" ", strip=True)
            if not re.fullmatch(r"[\u3400-\u9fff]{2,5}", name):
                continue
            url = urljoin(directory, a.get("href", ""))
            if url in profile_urls or not url_in_domain(url, domain):
                continue
            profile_urls.add(url)
            profiles.append((name, url, a.get_text(" ", strip=True)[:350]))
        # 只选择输入已写明的合作者/导师对应的人，不能盲读姓名列表前几人。
        picked_names = select(profiles) if select and profiles else [candidate_name]
        for name, url, _ in profiles:
            if name not in picked_names:
                continue
            body = get(url)
            if body:
                pages.append(SearchHit(url=url, title=f"{name} · 教师个人主页", content=body))
            if used >= max_reads:
                return pages
        if pages:
            break
    return pages
