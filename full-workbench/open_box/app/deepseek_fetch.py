"""Fetch public web pages through DeepSeek, retaining normal document readers."""
from __future__ import annotations
import html
import os
import ipaddress
import re
from urllib.parse import urlparse
from .search import PageFetcher, robots_allows
from .deepseek_client import DeepSeekClient, DeepSeekError

_DOCUMENT = re.compile(r"\.(?:pdf|docx?|xlsx?|pptx?|zip|rar|7z|png|jpe?g|gif|webp)(?:$|[?#])", re.I)
_LINK = re.compile(r"\[([^\]\n]{1,300})\]\((https?://[^\s)]+|/[^\s)]*)\)")


def _html_from_markdown(markdown):
    def inline(line):
        pieces, position = [], 0
        for match in _LINK.finditer(line):
            pieces.append(html.escape(line[position:match.start()]))
            pieces.append('<a href="' + html.escape(match.group(2), quote=True) + '">' +
                          html.escape(match.group(1)) + '</a>')
            position = match.end()
        pieces.append(html.escape(line[position:]))
        return "".join(pieces)
    lines, title = [], ""
    for line in markdown.splitlines():
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            level = len(heading.group(1))
            text = heading.group(2).strip()
            if not title:
                title = text
            lines.append(f"<h{level}>{inline(text)}</h{level}>")
        elif line.strip():
            lines.append("<p>" + inline(line) + "</p>")
    return ("<html><head><title>" + html.escape(title) + "</title></head><body><article>" +
            "".join(lines) + "</article></body></html>")


def _public_url(url):
    try:
        parts = urlparse(url)
        if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
            return False
        host = parts.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host
    except ValueError:
        return False


class DeepSeekPageFetcher(PageFetcher):
    def get_roster(self, url, claim):
        # Official result pages can contain inline images and attachment links
        # omitted by the provider's Markdown conversion. Keep their raw HTML.
        if claim.category in {"竞赛", "学历", "校内荣誉", "奖学金"} and _public_url(url):
            text = super().get(url, archive_hint=str(claim.raw_text) + str(claim.entities))
            if text:
                return text
        return super().get_roster(url, claim)

    def get(self, url: str, *, archive_hint: str = "", publication_catalog: bool = False):
        if not _public_url(url):
            self.failures[url] = "不是公开网页地址"
            return None
        # Attachment/roster parsers require the original bytes and specialized logic.
        if _DOCUMENT.search(url) or publication_catalog:
            return super().get(url, archive_hint=archive_hint, publication_catalog=publication_catalog)
        host = urlparse(url).hostname or ""
        result = None
        with self._host_lock(host):
            if not robots_allows(url):
                self.failures[url] = "robots.txt 不允许访问"
                return None
            self._throttle(host)
            try:
                result = DeepSeekClient().fetch(url)
                self.resolved_urls[url] = url
                self.failures.pop(url, None)
            except DeepSeekError as exc:
                if exc.status == 403:
                    self.failures[url] = "网页读取被安全策略拒绝"
                    return None
            except ValueError:
                pass
        if result:
            return _html_from_markdown(result[:1000000])
        return super().get(url, archive_hint=archive_hint, publication_catalog=publication_catalog)


def build_fetcher(**kwargs):
    from .retrieval_policy import enabled
    if enabled():
        # ICT follows the historical direct reader; no paid fetch-first retry.
        return PageFetcher(**kwargs)
    if os.environ.get("DEEPSEEK_FETCH_ENABLED", "1") == "0":
        return PageFetcher(**kwargs)
    return DeepSeekPageFetcher(**kwargs)
