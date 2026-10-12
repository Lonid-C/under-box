"""Company contacts are manual-review aids, never candidate internship evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import re
from urllib.parse import quote, urlparse

from .schema import VerifiedClaim


def company_name(claim):
    values = [claim.entities.get(k) for k in ("company", "org")]
    values += [e.partition("=")[2] for e in claim.elements
               if e.partition("=")[0].strip() in {"单位", "公司", "实习单位", "机构"}]
    for value in values:
        if (isinstance(value, str) and 2 <= len(value.strip()) <= 160
                and not re.search(r"[<>\n]|https?://", value)
                and _compact(value) in _compact(claim.raw_text)):
            return value.strip()
    return ""


def _compact(value):
    return re.sub(r"\s+", "", value).replace("(", "（").replace(")", "）").casefold()


def query_url(company):
    return "https://www.qcc.com/web/search?key=" + quote(company, safe="") if company else ""


def firm_url(url):
    try:
        parsed = urlparse(url)
        return (parsed.scheme == "https" and parsed.hostname in {"qcc.com", "www.qcc.com", "m.qcc.com"}
                and not parsed.username and not parsed.password and parsed.port in (None, 443)
                and re.fullmatch(r"/firm/[a-zA-Z0-9_-]+\.html", parsed.path) is not None)
    except ValueError:
        return False


def _page_company(html, company):
    """Require the company to be the exact page subject, not a related company.

    This intentionally doesn't resolve short brand names into invented legal
    entities. In that case the search link remains available for human selection.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    names = [node.get_text(" ", strip=True) for node in soup.select("h1")]
    if names:
        return _compact(company) in {_compact(name) for name in names}
    if soup.title:
        names.append(re.split(r"\s*[-_|｜]\s*", soup.title.get_text(" ", strip=True))[0])
    return _compact(company) in {_compact(name) for name in names}


PHONE = re.compile(r"(?<![\d*])(?:\+?86[ -]?)?(?:1[3-9]\d{9}|0\d{2,3}[ -]?\d{7,8}|400[ -]?\d{3}[ -]?\d{4}|800[ -]?\d{3}[ -]?\d{4})(?![\d*])")


def public_phones(html, company):
    from bs4 import BeautifulSoup
    if not _page_company(html, company):
        return []
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("head,title,script,style,nav,footer,header,[hidden],[aria-hidden='true']"):
        node.decompose()
    for node in list(soup.select("[style]")):
        if node.parent and re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", node.get("style", ""), re.I):
            node.decompose()
    # Remove site/customer-service and related-company blocks before extraction.
    for node in list(soup.select("section,aside")):
        if node.parent and re.search(r"企查查客服|客服电话|相关推荐|相关企业", node.get_text(" ", strip=True)):
            node.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.split(r"相关推荐|相关企业|推荐企业|你可能感兴趣", text)[0]
    contacts = []
    for match in re.finditer(r"(?:联系电话|公司电话|企业电话|电话|Telephone|Phone)\s*[:：]?\s*([^\n]{0,100})", text, re.I):
        excerpt = match.group(0).strip()
        before = text[:match.start()].rsplit("\n", 1)[-1]
        if re.search(r"客服|服务热线|传真|个人|企查查", before, re.I):
            continue
        for phone in PHONE.finditer(match.group(1)):
            value = phone.group()
            if value not in [p["phone"] for p in contacts]:
                contacts.append({"phone": value, "excerpt": excerpt, "kind": "公司公开联系电话"})
    return contacts[:3]


def build_manual_review(claim, searcher, fetcher, budget, progress=lambda *_: None):
    company = company_name(claim)
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    review = {"kind": "internship", "company": company, "queryUrl": query_url(company),
              "companyUrl": "", "contacts": [], "state": "company_name_missing",
              "checkedAt": checked_at,
              "note": "公司页面及电话仅用于人工背调，不能证明本人实习；公开公司电话未必是人力资源或背调专线。",
              "checks": ["确认公司全称和实际实习主体", "联系公司转接人力资源或实习负责人",
                         "分别核对本人实习岗位、起止时间及职责"], "limitations": []}
    if company:
        review["state"] = "link_only"
        hits = []
        if budget.can_search():
            budget.note_search()
            progress("按公司名称定位企查查页面（不检索候选人姓名）")
            try:
                hits = searcher.search('"' + company.replace('"', '') + '"', site="qcc.com")
            except Exception:
                review["limitations"].append("公司检索暂不可用，保留企查查查询入口供手动打开。")
        seen = set()
        reads = 0
        for hit in hits:
            if not firm_url(hit.url) or hit.url in seen:
                continue
            seen.add(hit.url)
            # Search results mentioning the company only in a related-party
            # paragraph must not spend the page-read budget.
            subject = re.split(r"\s*[-_|｜]\s*", hit.title)[0]
            if _compact(subject) != _compact(company):
                continue
            if reads >= 2 or not budget.can_read():
                break
            budget.note_read()
            reads += 1
            try:
                html = fetcher.get(hit.url)
            except Exception:
                html = None
            resolved = getattr(fetcher, "resolved_urls", {}).get(hit.url, hit.url)
            if not html or not firm_url(resolved) or not _page_company(html, company):
                continue
            review["companyUrl"] = resolved
            review["state"] = "company_page_found"
            review["contacts"] = [{**p, "sourceUrl": resolved, "accessedAt": checked_at}
                                  for p in public_phones(html, company)]
            if review["contacts"]:
                review["state"] = "contact_found"
                break
        if not review["contacts"]:
            review["limitations"].append("未取得可与该公司主体对应的完整公开电话；可打开企查查查询入口确认。登录、验证码或访问限制时不推测号码。")
    else:
        review["limitations"].append("输入未明确实习公司名称，请补充公司全称后查询。")
    return VerifiedClaim(claim=claim, status="none", evidence=[], proved=[], unproved=list(claim.elements),
                         needs_human=True, search_exhausted=budget.exhausted, manual_review=review,
                         next_step="打开对应公司的企查查页面，确认公司主体后联系公开电话转接实习负责人，人工核对岗位和时间。",
                         question="能否提供实习公司的完整名称及可核对岗位、时间的材料？")
