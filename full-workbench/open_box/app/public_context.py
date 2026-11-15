"""Source-bound institutional background. These records never enter judge.assess."""
from __future__ import annotations

from datetime import datetime, timezone
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .homepage import normalize_url
from .parse import detect_injection, scrub


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def public_url(value):
    try:
        if not isinstance(value, str) or not value.startswith(("https://", "http://")):
            return ""
        value = normalize_url(value)
        parsed = urlparse(value)
        return value if parsed.port in (None, 80, 443) else ""
    except (ValueError, RuntimeError):
        return ""


def compact(value):
    return re.sub(r"[\s（）()·_-]+", "", str(value or "")).casefold()


def visible_text(html):
    soup = BeautifulSoup((html or "")[:1_000_000], "html.parser")
    for node in soup.select("script,style,noscript,template,iframe,svg,nav,[hidden],[aria-hidden='true']"):
        node.decompose()
    for node in list(soup.select("[style]")):
        if node.parent and re.search(r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:;|$)", node.get("style", ""), re.I):
            node.decompose()
    text = soup.get_text("\n", strip=True)
    return scrub(text, detect_injection(text))[:40000]


def source_field(label, value, excerpt, url, title="", method="text"):
    return {"label": label, "value": value[:600], "excerpt": excerpt[:650],
            "sourceUrl": url, "sourceTitle": title[:180], "accessedAt": now(),
            "extractionMethod": method}


def labeled_fields(text, labels, url, title="", method="text"):
    """Copy labeled values literally; never infer size from capital or invent a summary."""
    all_labels = [alias for aliases in labels.values() for alias in aliases]
    stop = r"(?:\s+(?:" + "|".join(map(re.escape, all_labels)) + r")\s*[:：])"
    fields = {}
    for key, aliases in labels.items():
        for alias in aliases:
            pattern = r"(?:^|[\n；;。])\s*" + re.escape(alias) + r"\s*[:：]?\s*([^\n]{2,600})"
            for match in re.finditer(pattern, text, re.I):
                value = re.split(stop, match.group(1))[0].strip(" ：:")
                if not value or value in all_labels or any(re.match(re.escape(label) + r"\s*[:：]", value) for label in all_labels) or re.search(r"登录|验证码|暂无|未披露|查看全部|点击查看|--", value):
                    continue
                fields[key] = source_field(alias, value, match.group(0).strip(), url, title, method)
                break
            if key in fields:
                break
    return fields


EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
PHONE = re.compile(r"(?<![\d*])(?:\+?86[ -]?)?(?:0\d{2,3}[ -]?\d{7,8}|400[ -]?\d{3}[ -]?\d{4}|800[ -]?\d{3}[ -]?\d{4}|1[3-9]\d{9})(?![\d*])")


def public_contacts(text, url, *, kind, allow_mobile=False, allow_service=False):
    """Only contact-labeled blocks; no candidate contacts, fax, or masked numbers."""
    contacts, seen = [], set()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        labels = r"电话|总机|联系方式|联系邮箱|电子邮[件箱]|邮箱|E-?mail|Telephone|Phone|招生咨询|教务办"
        if allow_service:
            labels += r"|服务热线|客服热线"
        if not re.search(labels, line, re.I):
            continue
        if (re.search(r"技术支持|个人手机|私人", line, re.I) or (not allow_service and "客服" in line)) or (
                "传真" in line and not re.search(r"电话|Telephone|Phone", line, re.I)):
            continue
        block = " ".join(lines[max(0, i-1):i+3])[:450]
        # A following fax label is not another contact.
        candidate = re.sub(r"电话\s*[/／、及和]\s*传真|传真\s*[/／、及和]\s*电话",
                           "电话", " ".join(lines[i:i+3]))
        candidate = re.split(r"传真|技术支持" + ("" if allow_service else "|客服"), candidate)[0]
        values = [("email", m.group()) for m in EMAIL.finditer(candidate)]
        values += [("phone", m.group()) for m in PHONE.finditer(candidate)
                   if allow_mobile or not re.fullmatch(r"(?:\+?86[ -]?)?1[3-9]\d{9}", m.group())]
        for typ, value in values:
            if (typ, value) in seen:
                continue
            seen.add((typ, value))
            contacts.append({"kind": kind, "type": typ, "value": value,
                             typ: value, "excerpt": block, "sourceUrl": url, "accessedAt": now()})
    return contacts[:6]


def search_safely(searcher, query, site, budget, limitations):
    if not budget.can_search():
        return []
    budget.note_search()
    try:
        from .api_config import background_search
        with background_search():
            return searcher.search(query, site=site)[:8]
    except Exception as exc:
        message = str(exc)
        if re.search(r"余额|资源包|insufficient|quota", message, re.I):
            limitations.append("搜索服务余额或资源包不足；本次未取得资料，请联系管理员检查搜索配置。")
        else:
            limitations.append("本次部分检索不可用，保留公开查询入口；检索失败不表示资料不存在。")
        return []


def read_safely(fetcher, url, budget):
    if not public_url(url) or not budget.can_read():
        return None, ""
    budget.note_read()
    try:
        html = fetcher.get(url)
        final = getattr(fetcher, "resolved_urls", {}).get(url, url)
        if not html or not public_url(final):
            return None, ""
        # Login/captcha shells have no source content. Keep the original search
        # result as a clearly labeled snippet instead of citing a login redirect.
        path = urlparse(final).path.lower()
        soup = BeautifulSoup(html[:100000], "html.parser")
        heading = " ".join(n.get_text(" ", strip=True) for n in soup.select("title,h1"))
        if (re.search(r"(?:^|[/_-])(?:login|signin|tologin|captcha|passport)(?:[/_.-]|$)", path)
                or re.search(r"请先登录|用户登录|安全验证|人机验证|访问验证|访问受限|验证码", heading)):
            return None, ""
        return html, final
    except Exception:
        return None, ""
