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
        return (parsed.scheme == "https" and parsed.hostname in {"qcc.com", "www.qcc.com", "m.qcc.com", "m-harmony.qcc.com"}
                and not parsed.username and not parsed.password and parsed.port in (None, 443)
                and re.fullmatch(r"/firm/[a-zA-Z0-9_-]+\.html", parsed.path) is not None)
    except ValueError:
        return False


def _owned_company_page(html, company):
    """A copyright owner can bind a branded website to its exact legal entity."""
    from bs4 import BeautifulSoup
    from .public_context import visible_text
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select('footer,[class*="footer"],[id*="footer"]'):
        text = _compact(visible_text(str(node)))
        name = _compact(company)
        if name in text and re.search(r"(?:版权所有.{0,120}" + re.escape(name) +
                r"|" + re.escape(name) + r".{0,35}版权所有|(?:copyright|©).{0,150}" + re.escape(name) + r")", text, re.I):
            return True
    return False


def _page_company(html, company):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    names = [node.get_text(" ", strip=True) for node in soup.select("h1")]
    if names and any(_compact(name) == _compact(company) for name in names):
        return True
    # An explicitly different legal entity in the heading wins over footer hints.
    if any(re.search(r"有限公司|有限责任公司", name) for name in names):
        return False
    if _owned_company_page(html, company):
        return True
    if names:
        return False
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



COMPANY_FIELDS = {
    "introduction": ("公司简介", "企业简介", "简介", "公司介绍"),
    "registeredAddress": ("注册地址", "注册办事处地址", "注册办事处", "住所"),
    "officeAddress": ("办公地址", "联系地址", "公司地址", "办公地点"),
    "establishedAt": ("成立日期", "成立时间", "注册成立日期"),
    "registrationNumber": ("统一社会信用代码", "公司注册编号", "商业登记号码"),
    "companyType": ("企业类型", "公司类型"),
    "registeredCapital": ("注册资本",),
    "registrationStatus": ("登记状态", "经营状态"),
    "businessScope": ("经营范围",),
    "size": ("公司规模", "企业规模", "人员规模", "员工人数", "参保人数"),
    "mainBusiness": ("主营业务", "主要业务", "主营产品", "业务范围"),
    "legalRepresentative": ("法定代表人", "法人代表"),
    "contactPerson": ("联系人", "联系人员"),
}


def registry_url(url):
    if firm_url(url):
        return "企查查"
    try:
        parsed = urlparse(url)
        if (parsed.scheme == "https" and parsed.hostname in
                {"tianyancha.com", "www.tianyancha.com", "m.tianyancha.com"}
                and not parsed.username and not parsed.password and parsed.port in (None, 443)
                and re.fullmatch(r"/company/[a-zA-Z0-9_-]+(?:/sifa)?/?", parsed.path)):
            return "天眼查"
    except ValueError:
        pass
    return ""


def related_company_names(claim):
    """Only explicitly named host companies; aliases are never resolved by guessing."""
    names = []
    values = claim.entities.get("host_companies") or []
    if isinstance(values, str):
        values = [values]
    elif not isinstance(values, (list, tuple)):
        values = []
    match = re.search(r"(?:驻企于|驻企单位|合作企业)\s*[:：]?\s*([^\n。；;]{2,240})", claim.raw_text)
    if match:
        values = [*values, *re.split(r"[、，,]", match.group(1))]
    primary = company_name(claim)
    for value in values:
        name = str(value).strip()
        if (name != primary and name in claim.raw_text and len(name) <= 100
                and re.fullmatch(r"[\w\u3400-\u9fff（）()·-]{2,}(?:有限公司|有限责任公司)", name)
                and name not in names):
            names.append(name)
    return names[:3]


def build_manual_review(claim, searcher, fetcher, budget, progress=lambda *_: None, *, _include_related=True):
    from .public_context import (now, public_url, visible_text, labeled_fields, source_field,
                                 public_contacts, search_safely, read_safely)
    company = company_name(claim)
    checked_at = now()
    review = {"kind": "internship", "company": company, "queryUrl": query_url(company),
              "tianyanchaQueryUrl": "https://www.tianyancha.com/search?key=" + quote(company, safe="") if company else "",
              "companyUrl": "", "registryLinks": [], "contacts": [], "profile": {},
              "state": "company_name_missing", "checkedAt": checked_at,
              "note": "企业资料及公开办公联系方式仅供人工背调，不能证明候选人的任职/实习。企业规模、参保人数与注册资本是不同指标；法定代表人未必是人力资源或背调联系人。",
              "checks": ["确认公司全称和实际实习主体（品牌名不等同于具体法人）",
                         "联系公开办公渠道转接人力资源或实习负责人",
                         "分别核对本人实习岗位、起止时间及职责"], "limitations": []}
    if company:
        review["state"] = "link_only"
        def candidates():
            # Consume hits between searches so slow search calls cannot starve page reads.
            for site in ("qcc.com", "tianyancha.com"):
                progress("定位" + company + "的" + ("企查查" if site == "qcc.com" else "天眼查") + "资料")
                yield from search_safely(searcher, '"' + company.replace('"', '') + '"', site,
                                        budget, review["limitations"])
            yield from search_safely(searcher, company + " 官网 联系方式 注册地址 经营范围", None,
                                    budget, review["limitations"])
        seen = set()
        for hit in candidates():
            if not public_url(hit.url) or hit.url in seen:
                continue
            seen.add(hit.url)
            title_subject = re.split(r"\s*[-_|｜]\s*", hit.title)[0]
            exact_subject = _compact(title_subject) == _compact(company)
            registry = registry_url(hit.url)
            if registry:
                suffix = _compact(title_subject).removeprefix(_compact(company))
                exact_subject = exact_subject or (
                    _compact(title_subject).startswith(_compact(company)) and
                    suffix in {"工商信息", "企业信息", "公司简介", "联系方式", "法律诉讼"})
            # Never resolve a brand, subsidiary or related-company mention into a legal entity.
            if not exact_subject and (registry or _compact(company) not in _compact(hit.snippet or "")):
                continue
            if registry:
                review["registryLinks"].append({"title": registry + "企业详情（搜索命中，主体待回读）",
                                                "url": hit.url, "verifiedSubject": False})
            html, resolved = read_safely(fetcher, hit.url, budget)
            if not exact_subject and (not html or not _page_company(html, company)):
                continue
            method = "text"
            if not html or not _page_company(html, company) or (registry and registry_url(resolved) != registry):
                # Search snippets remain visible as lower-confidence leads, with their provenance.
                text = (hit.snippet or "")[:3000]
                if _compact(company) not in _compact(text):
                    continue
                resolved, method = hit.url, "search_snippet"
            else:
                text = re.split(r"相关推荐|相关企业|推荐企业|你可能感兴趣", visible_text(html))[0]
                if registry:
                    review["companyUrl"] = review["companyUrl"] or resolved
                    review["state"] = "company_page_found"
                    review["registryLinks"][-1].update(title=registry + "企业详情", url=resolved, verifiedSubject=True)
                # Contacts are only extracted from a subject-confirmed page, never a snippet.
                review["contacts"].extend(public_contacts(text, resolved, kind="公司公开办公联系方式",
                                                          allow_mobile=True, allow_service=_owned_company_page(html, company)))
            text = re.split(r"相关推荐|相关企业|推荐企业|你可能感兴趣", text)[0]
            fields = labeled_fields(text, COMPANY_FIELDS, resolved, hit.title, method)
            if "introduction" not in fields:
                for line in text.splitlines():
                    if (35 <= len(line) <= 800 and _compact(company) in _compact(line)
                            and re.search(r"成立|专注|致力|是一家|主要从事", line)):
                        fields["introduction"] = source_field("公司介绍（原文节选）", line[:500], line[:600],
                                                               resolved, hit.title, method)
                        break
            for key, field in fields.items():
                previous = review["profile"].get(key)
                if not previous or previous["extractionMethod"] == "search_snippet" and method == "text":
                    review["profile"][key] = field
        seen_contacts = set()
        review["contacts"] = [c for c in review["contacts"] if not (
            (c["type"], c["value"]) in seen_contacts or seen_contacts.add((c["type"], c["value"])))][:6]
        if review["contacts"]:
            review["state"] = "contact_found"
        if not review["companyUrl"]:
            review["limitations"].append("未取得主体已回读的企业详情页；企查查/天眼查查询入口供人工选择。登录、验证码、robots 或访问限制时不绕过。")
        if not review["contacts"]:
            review["limitations"].append("未取得与该企业主体对应的公开办公电话或邮箱；不推测联系人或号码。")
        if any(f["extractionMethod"] == "search_snippet" for f in review["profile"].values()):
            review["limitations"].append("部分企业资料仅为搜索摘要，尚未回读原文，请打开来源核对最新登记信息。")
        review["missingFields"] = [aliases[0] for key, aliases in COMPANY_FIELDS.items()
                                    if key not in review["profile"]]
    else:
        review["limitations"].append("输入未明确公司名称，请补充公司全称后查询。")
    if _include_related:
        review["relatedCompanies"] = []
        for name in related_company_names(claim):
            host = claim.model_copy(update={"entities": {**claim.entities, "company": name}})
            child = build_manual_review(host, searcher, fetcher, budget, progress, _include_related=False).manual_review
            child["relationship"] = "驻企单位"
            review["relatedCompanies"].append(child)
    return VerifiedClaim(claim=claim, status="none", evidence=[], proved=[], unproved=list(claim.elements),
                         needs_human=True, search_exhausted=budget.exhausted, manual_review=review,
                         next_step="打开企业资料和公开联系渠道，确认公司主体后转接实习负责人，人工核对岗位和时间。",
                         question="能否提供实习公司的完整名称及可核对岗位、时间的材料？")
