import unittest
from unittest.mock import patch
import httpx
from app.deepseek_search import DeepSeekSearcher, FailureOnlyFallbackSearcher
from app.search import SearchUnavailable

class DeepSeekSearchTests(unittest.TestCase):
    def test_results_and_domain_filter(self):
        req = httpx.Request("POST", "https://api.deepseek.com/v1/tools/search")
        response = httpx.Response(200, request=req, json={"results": [
            {"url": "https://school.edu.cn/a", "title": "A", "snippet": "text"},
            {"url": "https://other.example/a", "title": "B"}]})
        with patch("app.deepseek_search.httpx.post", return_value=response) as post:
            hits = DeepSeekSearcher("test").search("x" * 200, "school.edu.cn")
        self.assertEqual([x.title for x in hits], ["A"])
        self.assertLessEqual(len(post.call_args.kwargs["json"]["query"]), 128)

    def test_empty_does_not_trigger_backup(self):
        class Searcher:
            def search(self, *args, **kwargs): return []
        class Backup:
            def search(self, *args, **kwargs): raise AssertionError("unexpected fallback")
        self.assertEqual(FailureOnlyFallbackSearcher(Searcher(), Backup()).search("x"), [])

    def test_default_prefers_deepseek_with_account_key_as_fallback(self):
        import os
        from app.api_config import SearchCredentials, bind_search_credentials
        from app.search import build_searcher, ZhipuSearcher
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test", "SEARCH_PROVIDER":"deepseek"}, clear=True), \
             bind_search_credentials(SearchCredentials("account-key")), \
             patch("app.deepseek_client.DeepSeekClient.search", return_value=[]), \
             patch.object(ZhipuSearcher, "search", return_value=[]) as backup:
            self.assertEqual(build_searcher().search("q"), [])
            backup.assert_not_called()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test", "SEARCH_PROVIDER":"deepseek"}, clear=True), \
             bind_search_credentials(SearchCredentials("account-key")), \
             patch("app.deepseek_client.DeepSeekClient.search", side_effect=ValueError("failed")), \
             patch.object(ZhipuSearcher, "search", return_value=["fallback"]) as backup:
            self.assertEqual(build_searcher().search("q"), ["fallback"])
            backup.assert_called_once()

    def test_failure_triggers_backup(self):
        class Primary:
            def search(self, *args, **kwargs): raise SearchUnavailable("unavailable")
        class Backup:
            def search(self, *args, **kwargs): return ["backup"]
        self.assertEqual(FailureOnlyFallbackSearcher(Primary(), Backup()).search("x"), ["backup"])
