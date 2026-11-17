"""DeepSeek search adapter preserving the verification pipeline's search contract."""
from __future__ import annotations
import os
import re
import httpx
from .search import SearchFiltered, SearchHit, SearchUnavailable, url_in_domain

_CONTENT_FILTER = re.compile(
    r"content[\s_-]*(?:filter(?:ed)?|risk)|content\s+exists\s+risk|"
    r"内容[^,，。;；]{0,12}(?:审核|风险|过滤)|审核(?:未通过|拒绝|拦截)",
    re.I)


class DeepSeekSearcher:
    def __init__(self, api_key="", timeout=20.0):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.timeout = timeout
        # 401/402 are account-level failures; keep using the configured fallback
        # for the rest of this report instead of paying the 402 latency per query.
        self._account_unavailable = False

    def search(self, query, site=None):
        if not self.api_key:
            raise SearchUnavailable("DeepSeek search is not configured")
        if self._account_unavailable:
            raise SearchUnavailable("DeepSeek search is unavailable")
        query = re.sub(r"\s+", " ", query).strip()
        if site:
            if not re.fullmatch(r"[A-Za-z0-9.-]+", site) or len(site) > 253:
                raise SearchUnavailable("Invalid search domain")
            suffix = " site:" + site
            if len(suffix) >= 128:
                raise SearchUnavailable("Invalid search domain")
            query = query[:128-len(suffix)].rstrip() + suffix
        else:
            query = query[:128]
        if not query:
            return []
        from .deepseek_client import DeepSeekClient, DeepSeekError
        from .cost_budget import BudgetLimit
        try:
            items = DeepSeekClient(self.api_key).search(query)
            hits = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                url = item.get("url")
                if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                    continue
                if site and not url_in_domain(url, site):
                    continue
                hits.append(SearchHit(url=url, title=str(item.get("title") or ""),
                                      snippet=str(item.get("snippet") or ""),
                                      summary=str(item.get("summary") or ""),
                                      publisher=str(item.get("site_name") or "")))
            return hits
        except BudgetLimit:
            return []
        except DeepSeekError as exc:
            if exc.status in (401, 402):
                self._account_unavailable = True
            if exc.status == 400 and _CONTENT_FILTER.search(getattr(exc, "detail", "")):
                raise SearchFiltered("DeepSeek search content filter", query=query, site=site) from exc
            raise SearchUnavailable("DeepSeek search unavailable") from exc
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise SearchUnavailable("DeepSeek search unavailable") from exc


class FailureOnlyFallbackSearcher:
    def __init__(self, primary, secondary):
        self.primary, self.secondary = primary, secondary

    def search(self, query, site=None):
        try:
            return self.primary.search(query, site=site)
        except SearchFiltered:
            # Content review is query-specific; rephrasing belongs to the caller,
            # not to the service-failure fallback index.
            raise
        except SearchUnavailable:
            if self.secondary is None:
                raise
            return self.secondary.search(query, site=site)
