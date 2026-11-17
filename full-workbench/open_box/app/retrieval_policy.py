"""Opt-in ICT recall improvements; proof rules remain in the original judge."""
import os
import threading
import time
from .quota_policy import unlimited


def enabled():
    return os.environ.get('UNDERBOX_ICT_RECALL') == '1'


def expand_budget(budget, search_ceiling, read_ceiling):
    if not enabled():
        return budget
    # Monetary budget controls the report. These are secondary per-claim rails.
    return {**budget, 'searches': min(100, budget['searches'] * 12),
            'reads': min(40, budget['reads'])}


class RecallFallbackSearcher:
    """Preserve the primary index; try another index at most four times per claim.

    The wrapper lives for one report. Empty alternate results are cached too, so
    confirmation and query variants cannot repeatedly charge for the same query.
    Account-exclusive credentials never enter this wrapper.
    """
    def __init__(self, primary, secondary, limit=4):
        self.primary, self.secondary, self.limit = primary, secondary, limit
        self._lock = threading.RLock()
        self._counts = {}
        self._cache = {}
        self._scope_locks = {}

    def __getattr__(self, name):
        return getattr(self.primary, name)

    def _alternate(self, query, site, error=None):
        from .cost_budget import alternate_allowed
        from .api_config import is_background_search
        from .data_sources import _ACTIVE_RUN, safe_reference
        from .search import SearchUnavailable, url_in_domain
        if is_background_search() or not alternate_allowed():
            if error:
                raise error
            return []
        run = _ACTIVE_RUN.get()
        scope = run.claim.id if run else 'unscoped'
        key = (scope, query, site)
        with self._lock:
            scope_lock = self._scope_locks.setdefault(scope, threading.RLock())
        with scope_lock:
            if key in self._cache:
                return list(self._cache[key])
            if not unlimited() and self._counts.get(scope, 0) >= self.limit:
                if error:
                    raise error
                return []
            self._counts[scope] = self._counts.get(scope, 0) + 1
            started = time.monotonic()
            event = {'sourceId': 'public_web', 'sourceName': '备用公开检索', 'action': 'alternate_search',
                     'provider': type(self.secondary).__name__, 'reference': safe_reference(query)}
            try:
                hits = self.secondary.search(query, site=site)
                if site:
                    hits = [hit for hit in hits if url_in_domain(hit.url, site)]
                event.update(state='returned' if hits else 'empty', records=len(hits))
            except SearchUnavailable as exc:
                event.update(state='failed', error=type(exc).__name__)
                if error:
                    raise error
                # The original index completed successfully; an optional backup
                # outage must not erase its valid empty response or earlier work.
                return []
            finally:
                event['seconds'] = round(time.monotonic() - started, 3)
                if run:run.add(event)
            self._cache[key] = list(hits)
            return list(hits)

    def search(self, query, site=None):
        from .search import SearchUnavailable
        try:
            hits = self.primary.search(query, site=site)
        except SearchUnavailable as exc:
            return self._alternate(query, site, exc)
        return hits if hits else self._alternate(query, site)

    def confirm_empty(self, query, site=None):
        from .search import SearchUnavailable
        confirm = getattr(self.primary, 'confirm_empty', None)
        try:
            hits = confirm(query, site=site) if callable(confirm) else []
        except SearchUnavailable as exc:
            return self._alternate(query, site, exc)
        return hits if hits else self._alternate(query, site)
