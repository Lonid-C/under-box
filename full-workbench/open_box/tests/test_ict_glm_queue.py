"""Rate-limit contracts exercised with synthetic accounts and HTTP responses."""
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
import os
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from app import zhipu_rate
from app.collect import Budget
from app.data_sources import AgentRun, SourceSearcher
from app.schema import Claim, VerifiedClaim
from app.search import SearchFiltered, ZhipuSearcher
from app.strategy import Plan, Query
from app.verification_agents import agent_for
from app.zhipu_rate import SearchQueue, ZhipuSearchError


def reply(status, code="", wait="", message=""):
    return httpx.Response(status, json={"error": {"code": code, "message": message}}
                          if status >= 400 else {"search_result": []}, headers={"Retry-After": wait})


class GlmQueueTests(unittest.TestCase):
    def setUp(self):
        rate = patch.multiple(zhipu_rate, QUEUE=SearchQueue(), MIN_INTERVAL=.005, RATE_BACKOFF=(.005,))
        rate.start()
        self.addCleanup(rate.stop)
        env = patch.dict(os.environ, {"UNDERBOX_ICT_GLM_QUEUE": "1", "UNDERBOX_ICT_UNLIMITED": "1"})
        env.start()
        self.addCleanup(env.stop)

    def test_backoff_applies_to_retry_and_other_searcher(self):
        entered = threading.Event()
        calls = []
        pending = peak = 0
        retry_after = .06

        def provider(*args, **kwargs):
            nonlocal pending, peak
            query = kwargs["json"]["search_query"]
            pending += 1
            peak = max(peak, pending)
            calls.append((query, time.monotonic()))
            if len(calls) == 1:
                entered.set()
            time.sleep(.005)
            pending -= 1
            return reply(429, "1302", str(retry_after)) if len(calls) == 1 else reply(200)

        a = ZhipuSearcher(api_key="synthetic")
        b = ZhipuSearcher(api_key="synthetic")
        with patch("httpx.post", side_effect=provider), ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(a.search, "query-a")
            self.assertTrue(entered.wait(2))
            f2 = pool.submit(b.search, "query-b")
            self.assertEqual(f1.result(timeout=2), [])
            self.assertEqual(f2.result(timeout=2), [])
        self.assertCountEqual([name for name, _ in calls], ["query-a", "query-a", "query-b"])
        self.assertEqual(peak, 1)
        self.assertGreaterEqual(calls[1][1] - calls[0][1], retry_after)

    def test_account_error_short_circuits_same_credential_and_keeps_other_account_usable(self):
        a = ZhipuSearcher(api_key="synthetic-first")
        another_instance = ZhipuSearcher(api_key="synthetic-first")
        b = ZhipuSearcher(api_key="synthetic-second")
        denied = reply(429, "1113", message="本次测试不应暴露 synthetic-first")
        with patch("httpx.post", side_effect=[denied, reply(200)]) as provider:
            with self.assertRaises(ZhipuSearchError) as first:
                a.search("one")
            with self.assertRaises(ZhipuSearchError) as subsequent:
                another_instance.search("two")
            self.assertEqual(b.search("three"), [])
        self.assertEqual(provider.call_count, 2)
        self.assertTrue(first.exception.sent)
        self.assertFalse(subsequent.exception.sent)
        self.assertEqual(subsequent.exception.code, "1113")
        self.assertNotIn("synthetic-first", str(first.exception))

    def test_long_server_retry_time_is_respected_and_logged_in_claim(self):
        claim = Claim(id="sample", category="论文", raw_text="论文甲", raw_locator="", date_label="")
        run = AgentRun(agent_for(claim), claim, Plan([Query("论文甲")], "sample", "sample"), Budget())
        source = SourceSearcher(ZhipuSearcher(api_key="synthetic"), run)
        with run.bind(), patch("httpx.post", return_value=reply(429, "1305", "150")) as provider:
            with self.assertRaises(ZhipuSearchError) as failed:
                source.search("论文甲")
        self.assertEqual(provider.call_count, 1)
        self.assertTrue(failed.exception.sent)
        result = VerifiedClaim(claim=claim, status="none")
        stats = run.finish(result)["sourceAccess"]
        self.assertEqual(stats["counts"]["provider_response"]["rate_limited"], 1)
        self.assertEqual(stats["events"][-1]["providerCode"], "1305")
        self.assertTrue(stats["events"][-1]["attempted"])
        self.assertEqual(stats["failureGroups"][0]["error"], "zhipu_1305")
        self.assertIn("服务繁忙", result.next_step)
        self.assertEqual(result.retrieval_status, "provider_error")

    def test_request_that_hits_content_filter_is_not_retried(self):
        with patch("httpx.post", return_value=reply(400, "1301")) as provider:
            with self.assertRaises(SearchFiltered):
                ZhipuSearcher(api_key="synthetic").search("query")
        self.assertEqual(provider.call_count, 1)

    def test_retry_after_accepts_http_date_and_rejects_invalid_values(self):
        future = formatdate(time.time() + 61, usegmt=True)
        self.assertGreater(zhipu_rate.retry_after_seconds(future), 59)
        self.assertEqual(zhipu_rate.retry_after_seconds("-1"), 0)
        self.assertEqual(zhipu_rate.retry_after_seconds("nan"), 0)
