"""Recovery for public sources with separate handling for access and evidence failures."""
import os
import re
from urllib.parse import unquote, urlsplit


def enabled():
    return os.environ.get("UNDERBOX_ICT_SOURCE_REPAIR") == "1"


def doi_from_url(url):
    path = unquote(urlsplit(url).path)
    pattern = r"(?:^|/)(10\.\d{4,9}/[^\s?#<>\"{}]+)$"
    match = re.search(pattern, path, re.I)
    return match[1] if match and len(match[1]) <= 200 else ""


def publication_page_doi(html, title):
    """Follow a DOI exposed by a matching publication page, never an unrelated reference."""
    if not title or '<' not in html[:2000]:
        return ''
    from bs4 import BeautifulSoup
    from .paper_identity import compact, _doi
    soup = BeautifulSoup(html, 'lxml')
    meta = soup.select_one('meta[name="citation_title"]')
    heading = soup.find('h1')
    titles = [meta.get('content', '') if meta else '', heading.get_text(' ', strip=True) if heading else '']
    if not any(compact(candidate) == compact(title) for candidate in titles):
        return ''
    declared = {_doi(tag.get('content')) for tag in soup.select('meta[name="citation_doi"]')} - {''}
    if declared:
        return next(iter(declared)) if len(declared) == 1 else ''
    links = {_doi(a.get('href')) for a in soup.select('a[href^="https://doi.org/"]')} - {''}
    return next(iter(links)) if len(links) == 1 else ''


def failed_entry(reason):
    if re.search(r"\b(404|410)\b", reason):
        return "moved"
    if re.search(r"\b(403|412|418|468)\b|证书|网络不可达|连接失败|连接超时", reason):
        return "unavailable"
    return ""
