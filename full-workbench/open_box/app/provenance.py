"""Rule-owned, bounded evidence passports; no full-page snapshots are retained.

Offsets refer to the parsed body supplied to extraction, not HTML byte offsets.
Only Unicode canonical normalization and whitespace are ignored. No fuzzy text,
case folding, punctuation removal or inferred source origins are permitted.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def canonical_url(value: str) -> str:
    try:
        u = urlsplit(value)
        if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password:
            return ""
        query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_")]
        return urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path or "/", urlencode(query), ""))
    except ValueError:
        return ""


def _normalized(value):
    chars, offsets = [], []
    # Group combining marks so canonical normalization preserves offset mapping.
    groups = []
    for index, char in enumerate(value):
        if unicodedata.combining(char) and groups:
            groups[-1][1] += char
            groups[-1][2] = index + 1
        else:
            groups.append([index, char, index + 1])
    for start, group, end in groups:
        for char in unicodedata.normalize("NFC", group):
            if not char.isspace():
                chars.append(char)
                offsets.append((start, end))
    return "".join(chars), offsets


def locate_quote(quote: str, body: str):
    if not quote or any(m in quote for m in ("［中间原文省略］", "[…]", "[...]", "［正文过长")):
        return None
    needle, _ = _normalized(quote)
    # Focus windows and parsed document separators must never form fake adjacency.
    import re
    boundaries = list(re.finditer(re.escape("［中间原文省略］"), body))
    start = 0
    segments = []
    for boundary in boundaries:
        segments.append((start, body[start:boundary.start()]))
        start = boundary.end()
    segments.append((start, body[start:]))
    for base, segment in segments:
        text, offsets = _normalized(segment)
        index = text.find(needle) if needle else -1
        if index >= 0:
            return base + offsets[index][0], base + offsets[index + len(needle) - 1][1]
    return None


def evidence_id(ev):
    return "ev-" + sha256((canonical_url(ev.url) + "\n" + ev.snippet).encode()).hexdigest()[:24]


def bind_page(ev, body: str, *, resolved_url: str = ""):
    """Called with the actual parsed body, never the model's provenance fields."""
    ev.evidence_id = evidence_id(ev)
    ev.original_url = ev.url
    ev.resolved_url = resolved_url or ev.url
    ev.fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ev.source_content_sha256 = sha256(body.encode()).hexdigest()
    location = locate_quote(ev.snippet, body)
    ev.quote_verified = location is not None
    ev.quote_start, ev.quote_end = location or (None, None)
    ev.source_kind = "page_body"
    ev.provenance_state = "page_text_verified" if location else "quote_rejected"
    ev.validation_reasons = ["引文已在本次解析正文定位" if location else "引文未在本次解析正文连续定位"]
    mark_unverified(ev)
    return ev


def mark_unverified(ev):
    if ev.extraction_method in {"ocr", "search_snippet"}:
        ev.provenance_state = {"ocr": "ocr_unverified", "search_snippet": "search_snippet_unverified"}[ev.extraction_method]
        ev.source_kind = ev.extraction_method
        ev.quote_verified = False
        ev.quote_start = ev.quote_end = None
    return ev


def usable_provenance(ev, claim=None):
    """Recheck saved contract before accepting support; historical display does not call this."""
    if ev.paper_record is not None:
        from .paper_identity import author_match
        return bool(claim and ev.provenance_state == "structured_metadata_verified"
                    and author_match(claim, ev.paper_record).allowed)
    if ev.github_record is not None:
        from .github_audit import parse_link
        rec = ev.github_record
        return bool(ev.provenance_state == "structured_metadata_verified" and parse_link(ev.url)
                    and rec.get("source") == "github_public_api" and rec.get("recordExists")
                    and rec.get("url") == ev.url)
    return bool(ev.extraction_method == "text" and ev.provenance_state == "page_text_verified"
                and ev.quote_verified and len(ev.source_content_sha256) == 64
                and ev.quote_start is not None and ev.quote_end is not None
                and 0 <= ev.quote_start < ev.quote_end and ev.evidence_id)


def bind_structured(ev):
    """Only rule adapters call this, after their own record/identity validation."""
    import json
    record = ev.paper_record if ev.paper_record is not None else ev.github_record
    ev.evidence_id = evidence_id(ev)
    ev.original_url = ev.resolved_url = ev.url
    ev.fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ev.source_content_sha256 = sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    ev.source_kind = "structured_metadata"
    ev.provenance_state = "structured_metadata_verified"
    ev.validation_reasons = ["结构字段经专项适配器映射，非网页逐字引用"]
    return ev
