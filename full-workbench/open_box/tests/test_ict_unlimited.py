"""Quota removal exercises counters beyond former ceilings, with synthetic sources only."""
import json,os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import httpx
from app.collect import Budget,collect_for_claim
from app.cost_budget import CostBudget,BudgetLimit,bind,search_allowed,ClaimShare
from app.data_sources import AgentRun
from app.github_audit import GitHubClient,Unavailable
from app.plan import Query
from app.quota_policy import unlimited
from app.schema import Claim,VerifiedClaim
from app.strategy import ResumeProfile,build_plan


def claim():
    return Claim(id='synthetic',category='学历',raw_text='合成测试甲在清华大学计算机专业本科在读',raw_locator='合成',date_label='2024',elements=['学校=清华大学','学段=本科'],entities={'name':'合成测试甲','school':'清华大学'})

class UnlimitedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.env=patch.dict(os.environ,{'UNDERBOX_ICT_UNLIMITED':'1','UNDERBOX_ICT_RECALL':'1','UNDERBOX_ICT_COST_BUDGET':'1','UNDERBOX_DB':str(Path(self.tmp.name)/'users.db'),'COLLECT_WORKERS':'1'})
        self.env.start();self.addCleanup(self.env.stop)
    def test_budget_continues_after_search_read_and_time_limits(self):
        b=Budget(max_searches=1,max_page_reads=1,max_seconds=1);b.started-=100000
        for _ in range(1101):
            self.assertTrue(b.can_search());b.note_search()
            self.assertTrue(b.can_read());b.note_read();b.finish_read(False)
        self.assertFalse(b.exhausted);self.assertEqual(b.failed_reads,1101)
    def test_ledger_continues_past_money_and_report_search_limit(self):
        b=CostBudget()
        with bind(b):
            for _ in range(1005):b.reserve(.02,'synthetic_search',search=True)
            self.assertTrue(search_allowed())
        self.assertGreater(b.spent,20);self.assertEqual(b.search_calls,1005)
        restored=CostBudget.load(b.id);self.assertFalse(restored.stopped);self.assertIsNone(restored.search_limit)
        self.assertTrue(restored.snapshot()['quotaLimitsDisabled']);json.dumps(restored.snapshot(),allow_nan=False)
    def test_prior_search_stop_clears_without_resetting_usage(self):
        with patch.dict(os.environ,{'UNDERBOX_ICT_UNLIMITED':'0','UNDERBOX_ICT_COST_BUDGET':'0'}):
            b=CostBudget();b.search_limit=1;b.reserve(.1,'synthetic',search=True)
            self.assertRaises(BudgetLimit,b.reserve,.1,'synthetic',search=True)
        restored=CostBudget.load(b.id);restored.reserve(.1,'synthetic',search=True)
        self.assertEqual(restored.search_calls,2);self.assertFalse(restored.stopped)
    def test_auxiliary_share_does_not_reintroduce_a_money_limit(self):
        root=CostBudget();share=ClaimShare(root,0)
        with bind(share):share.reserve(8,'synthetic',search=True);self.assertTrue(search_allowed())
        self.assertFalse(share.stopped);json.dumps(share.snapshot(),allow_nan=False)
    def test_collect_reads_all_finite_results_beyond_old_read_ceiling(self):
        from app.search import SearchHit
        search=SimpleNamespace(search=lambda *a,**k:[SearchHit('https://synthetic.example/'+str(i)) for i in range(55)])
        fetcher=Mock();fetcher.get.return_value='合成公开正文';fetcher.get_roster=None;fetcher.resolved_urls={};fetcher.failures={}
        b=Budget(max_searches=1,max_page_reads=1,max_seconds=1);b.started-=10000
        with patch('app.search.robots_allows',return_value=True),patch('app.collect.extract_evidence',return_value=None):
            collect_for_claim(claim(),'合成测试甲',search,object(),fetcher=fetcher,budget=b,queries=[Query('合成查询')])
        self.assertEqual(fetcher.get.call_count,55);self.assertFalse(b.exhausted)
    def test_plan_keeps_queries_and_report_has_json_null_limits(self):
        with patch.dict(os.environ,{'UNDERBOX_ICT_UNLIMITED':'0','UNDERBOX_ICT_RECALL':'0'}):
            limited=build_plan(claim(),'合成测试甲',ResumeProfile(identity='student'),resume_categories=['学历'],budget_searches=1)
        plan=build_plan(claim(),'合成测试甲',ResumeProfile(identity='student'),resume_categories=['学历'],budget_searches=1)
        self.assertGreater(len(plan.queries),len(limited.queries));self.assertFalse(plan.dropped)
        agent=SimpleNamespace(id='synthetic',describe=lambda:{'id':'synthetic'})
        run=AgentRun(agent,claim(),plan,Budget());run.add({'action':'search','state':'empty'})
        data=run.finish(VerifiedClaim(claim=claim(),status='none'))
        self.assertIsNone(data['runtimePlan']['budget']['reads']);self.assertTrue(data['runtimePlan']['budget']['unlimited'])
        for k in ('maxSearches','maxReads','maxSeconds','maxReadAttempts'):self.assertIsNone(data['sourceAccess']['budget'][k])
        json.dumps(data,allow_nan=False)
    def test_github_more_than_twenty_requests_and_three_pages(self):
        def response(request):
            page=int(request.url.params.get('page','1'))
            return httpx.Response(200,json=[{'page':page}],headers={'Link':'<https://api.github.com/repos/sample/one/commits?page='+str(page+1)+'>; rel="next"'} if page<5 else {})
        c=GitHubClient(transport=httpx.MockTransport(response),max_requests=1,max_seconds=0);self.addCleanup(c.close)
        for _ in range(21):c.get('/repos/sample/one')
        pages,truncated=c.pages('/repos/sample/one/commits',max_pages=1)
        self.assertEqual(len(pages),5);self.assertFalse(truncated);self.assertEqual(c.calls,26)
    def test_repeated_github_pages_still_fail_without_infinite_requests(self):
        c=GitHubClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=[1],headers={'Link':'<https://api.github.com/a?page=2>; rel="next"'})));self.addCleanup(c.close)
        self.assertRaises(Unavailable,c.pages,'/a');self.assertEqual(c.calls,2)
    def test_override_is_ict_only(self):
        with patch.dict(os.environ,{'UNDERBOX_ICT_ENABLE':'0','UNDERBOX_ICT_RECALL':'0'}):
            self.assertFalse(unlimited());b=Budget(max_searches=0);self.assertFalse(b.can_search())
