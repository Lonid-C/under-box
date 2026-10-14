"""Concurrent operation isolation and pricing remain stable across SSE lifetimes."""
import os
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from app.usage import UsageMeter, UsageExecutor, _is_peak

class UsageTests(unittest.TestCase):
    def test_parallel_nested_pools_keep_separate_usage_and_listeners(self):
        meter = UsageMeter()
        a, b = meter.open_scope(), meter.open_scope()
        seen = {a: [], b: []}
        unsub = [meter.subscribe(lambda s=s: seen[s].append(meter.snapshot(s)['glm']['searches']), s) for s in (a,b)]
        def job(scope, count):
            with meter.bind(scope):
                def nested(_):
                    with UsageExecutor(max_workers=2) as pool:
                        list(pool.map(lambda _: meter.record_search('search_std'), range(count)))
                with UsageExecutor(max_workers=2) as pool:
                    list(pool.map(nested, range(2)))
        with UsageExecutor(max_workers=2) as pool:
            list(pool.map(lambda args: job(*args), [(a,3),(b,5)]))
        self.assertEqual(meter.snapshot(a)['glm']['searches'],6)
        self.assertEqual(meter.snapshot(b)['glm']['searches'],10)
        self.assertEqual(len(seen[a]),6)
        self.assertEqual(len(seen[b]),10)
        self.assertEqual(meter.snapshot()['glm']['searches'],0)
        for fn in unsub: fn()
        meter.release_scope(a); meter.release_scope(b)
        self.assertEqual(set(meter._buckets), {'global'})

    def test_disconnected_reader_does_not_release_running_worker_bucket(self):
        meter=UsageMeter(); scope=meter.open_scope(owners=2)
        base=meter.mark(scope); meter.release_scope(scope)
        with meter.bind(scope): meter.record_search('search_pro')
        self.assertEqual(meter.since(base)['glm']['searches'],1)
        meter.run_in_scope(scope, lambda: meter.record_search('search_std'))
        self.assertNotIn(scope,meter._buckets)

    def test_reused_model_calls_belong_to_current_operation(self):
        meter=UsageMeter(); a,b=meter.open_scope(),meter.open_scope()
        record=lambda: meter.record_llm('deepseek',{'prompt_tokens':100,'completion_tokens':10})
        with meter.bind(a): record()
        with meter.bind(b): record(); record()
        self.assertEqual(meter.snapshot(a)['deepseek']['calls'],1)
        self.assertEqual(meter.snapshot(b)['deepseek']['calls'],2)

    def test_cost_is_recorded_at_call_time_not_repriced_on_read(self):
        meter=UsageMeter()
        prices={'DEEPSEEK_PRICE_IN':'2','DEEPSEEK_PRICE_IN_CACHED':'.04','DEEPSEEK_PRICE_OUT':'8',
                'DEEPSEEK_PRICE_IN_OFFPEAK':'1','DEEPSEEK_PRICE_IN_CACHED_OFFPEAK':'.02','DEEPSEEK_PRICE_OUT_OFFPEAK':'4'}
        with patch.dict(os.environ,prices), patch('app.usage._is_peak',return_value=True):
            meter.record_llm('deepseek',{'prompt_tokens':1000000,'prompt_cache_hit_tokens':500000,'completion_tokens':1000000})
        with patch.dict(os.environ,prices), patch('app.usage._is_peak',return_value=False):
            self.assertEqual(meter.snapshot()['deepseek']['yuan'],9.02)
            meter.record_llm('deepseek',{'prompt_tokens':1000000,'prompt_cache_hit_tokens':500000,'completion_tokens':1000000})
        self.assertEqual(meter.snapshot()['deepseek']['yuan'],13.53)

    def test_unknown_glm_model_price_does_not_look_like_search_only_total(self):
        meter=UsageMeter()
        with patch.dict(os.environ,{'ZHIPU_PRICE_IN':'','ZHIPU_PRICE_OUT':''}):
            meter.record_search('search_std')
            meter.record_llm('zhipu',{'prompt_tokens':10,'completion_tokens':3})
        d=meter.snapshot()['glm']
        self.assertEqual(d['searchYuan'],.01)
        self.assertIsNone(d['yuan'])
        self.assertEqual(d['tokens'],13)

    def test_peak_uses_beijing_even_if_given_utc(self):
        self.assertTrue(_is_peak(datetime(2026,10,9,1,0,tzinfo=timezone.utc)))
        self.assertFalse(_is_peak(datetime(2026,10,9,10,0,tzinfo=timezone.utc)))
