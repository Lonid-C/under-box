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


def failed_entry(reason):
    if re.search(r"\b(404|410)\b", reason):
        return "moved"
    if re.search(r"\b(403|412|418|468)\b|证书|网络不可达|连接失败|连接超时", reason):
        return "unavailable"
    return ""
