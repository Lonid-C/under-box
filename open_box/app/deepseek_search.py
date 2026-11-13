"""DeepSeek search adapter preserving the verification pipeline's search contract."""
from __future__ import annotations
import os
import re
import httpx
from .search import SearchHit, SearchUnavailable, url_in_domain

class DeepSeekSearcher:
    def __init__(self, api_key="", timeout=20.0):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.timeout = timeout

    def search(self, query, site=None):
        if not self.api_key:
            raise SearchUnavailable("DeepSeek search is not configured")
        query = re.sub(r"\s+", " ", query).strip()
        if site:
            if not re.fullmatch(r"[A-Za-z0-9.-]+", site):
                raise SearchUnavailable("Invalid search domain")
            suffix = " site:" + site
            query = query[:128-len(suffix)].rstrip() + suffix
        else:
            query = query[:128]
        if not query:
            return []
        try:
            r = httpx.post("https://api.deepseek.com/v1/tools/search",
                           headers={"Authorization": f"Bearer {self.api_key}"},
                           json={"query": query, "count": 10, "need_content": False},
                           timeout=self.timeout)
            r.raise_for_status()
            payload = r.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise ValueError("Invalid search response")
            hits = []
            for item in payload["results"]:
                if not isinstance(item, dict):
                    continue
                url = item.get("url")
                if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                    continue
                if site and not url_in_domain(url, site):
                    continue
                hits.append(SearchHit(url=url, title=str(item.get("title") or ""),
                                      snippet=str(item.get("snippet") or ""),
                                      publisher=str(item.get("site_name") or "")))
            return hits
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise SearchUnavailable("DeepSeek search unavailable") from exc

class FailureOnlyFallbackSearcher:
    def __init__(self, primary, secondary):
        self.primary, self.secondary = primary, secondary

    def search(self, query, site=None):
        try:
            return self.primary.search(query, site=site)
        except SearchUnavailable:
            if self.secondary is None:
                raise
            return self.secondary.search(query, site=site)
