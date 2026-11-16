"""Synthetic-only checks for broader retrieval and report-scoped CNY control."""
import os,tempfile,unittest,threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from app.collect import Budget,collect_for_claim,extract_evidence
from app.plan import Query,plan_with_notes
from app.strategy import ResumeProfile
from app.schema import Claim
from app.search import SearchHit,SearchFiltered,SearchUnavailable,build_searcher,ZhipuSearcher
from app.retrieval_policy import RecallFallbackSearcher
from app.api_config import SearchCredentials,bind_search_credentials
from app.cost_budget import CostBudget,BudgetLimit,bind,reserve_model
from app.deepseek_client import DeepSeekClient
from app.usage import METER,UsageExecutor


def claim(category='项目'):
    return Claim(id='synthetic',category=category,raw_text='合成测试甲在清华大学计算机专业本科在读',
                 raw_locator='合成摘录',date_label='2024',elements=['学校=清华大学','学段=本科'],
                 entities={'name':'合成测试甲','school':'清华大学'})


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'UNDERBOX_ICT_RECALL':'1','COLLECT_WORKERS':'1'});self.env.start();self.addCleanup(self.env.stop)
        self.robots=patch('app.search.robots_allows',return_value=True);self.robots.start();self.addCleanup(self.robots.stop)

    def collect(self,fetcher,hits,budget):
        search=SimpleNamespace(search=lambda *a,**k:hits)
        with patch('app.collect.extract_evidence',return_value=None):
            return collect_for_claim(claim(),'合成测试甲',search,object(),fetcher=fetcher,budget=budget,queries=[Query('合成查询')])

    def test_larger_plans_still_keep_finite_per_claim_rails(self):
        plan=plan_with_notes(claim('学历'),'合成测试甲',ResumeProfile(identity='student'),['学历'])
        self.assertEqual(plan.budget['searches'],96);self.assertEqual(plan.budget['reads'],40)
        self.assertLessEqual(plan.budget['seconds'],180)

    def test_blocked_robots_pages_do_not_consume_read_slots(self):
        hits=[SearchHit('https://blocked.example/'+str(i)) for i in range(10)]+[SearchHit('https://allowed.example/record')]
        fetcher=Mock();fetcher.get.return_value='合成公开正文';fetcher.get_roster=None;fetcher.resolved_urls={};fetcher.failures={}
        budget=Budget(max_searches=1,max_page_reads=2)
        with patch('app.search.robots_allows',side_effect=lambda u:'blocked.example' not in u):self.collect(fetcher,hits,budget)
        self.assertEqual(fetcher.get.call_count,1);self.assertEqual(budget.robots_skipped,10);self.assertEqual(budget.read_attempts,1)

    def test_failed_reads_receive_bounded_replacements(self):
        hits=[SearchHit('https://pages.example/'+str(i)) for i in range(12)]
        calls=[]
        def get(url):calls.append(url);return None if len(calls)<=2 else '合成公开正文'
        budget=Budget(max_searches=1,max_page_reads=4)
        self.collect(SimpleNamespace(get=get),hits,budget)
        self.assertEqual(len(calls),6);self.assertEqual(budget.successful_reads,4);self.assertEqual(budget.read_refunds,2)

    def test_all_failed_reads_cannot_loop_indefinitely(self):
        calls=[];budget=Budget(max_searches=1,max_page_reads=4)
        self.collect(SimpleNamespace(get=lambda u:calls.append(u)),[SearchHit('https://pages.example/'+str(i)) for i in range(20)],budget)
        self.assertEqual(len(calls),6);self.assertEqual(budget.max_read_attempts,6);self.assertTrue(budget.exhausted)

    def test_legacy_read_budget_has_no_refunds(self):
        budget=Budget(max_page_reads=2,recovery_enabled=False)
        for _ in range(2):budget.note_read();budget.finish_read(False)
        self.assertFalse(budget.can_read());self.assertEqual(budget.read_refunds,0)

    def test_time_limit_is_not_refunded(self):
        budget=Budget(max_page_reads=4,max_seconds=1);budget.note_read();budget.finish_read(False);budget.started-=2
        self.assertFalse(budget.can_read())

    def test_secondary_index_handles_primary_filtering(self):
        primary=Mock();primary.search.side_effect=SearchFiltered('synthetic filter')
        secondary=Mock();secondary.search.return_value=[SearchHit('https://school.example/record')]
        search=RecallFallbackSearcher(primary,secondary)
        self.assertEqual(len(search.search('合成查询')),1);self.assertEqual(secondary.search.call_count,1)

    def test_alternate_calls_are_limited_and_empty_results_cached(self):
        primary=Mock();primary.search.return_value=[];primary.confirm_empty.return_value=[]
        secondary=Mock();secondary.search.return_value=[]
        search=RecallFallbackSearcher(primary,secondary)
        search.search('same');search.confirm_empty('same')
        self.assertEqual(secondary.search.call_count,1)
        for i in range(10):search.search(str(i))
        self.assertEqual(secondary.search.call_count,4)

    def test_optional_index_outage_keeps_valid_primary_empty_result(self):
        primary=Mock();primary.search.return_value=[];secondary=Mock();secondary.search.side_effect=SearchUnavailable('synthetic outage')
        self.assertEqual(RecallFallbackSearcher(primary,secondary).search('q'),[])

    def test_alternate_domain_filter_cannot_be_bypassed(self):
        primary=Mock();primary.search.return_value=[];secondary=Mock();secondary.search.return_value=[SearchHit('https://other.example/wrong')]
        self.assertEqual(RecallFallbackSearcher(primary,secondary).search('q',site='school.example'),[])

    def test_account_specific_key_never_borrows_global_secondary(self):
        with patch.dict(os.environ,{'SEARCH_API_KEY':'global-synthetic','DEEPSEEK_API_KEY':'global-ds'}),bind_search_credentials(SearchCredentials('account-synthetic')):
            search=build_searcher('deepseek')
        self.assertIsInstance(search,ZhipuSearcher);self.assertEqual(search.api_key,'account-synthetic')

    def test_explicit_deepseek_selection_is_wired(self):
        with patch.dict(os.environ,{'SEARCH_API_KEY':'synthetic-zhipu','DEEPSEEK_API_KEY':'synthetic-ds'}):search=build_searcher('deepseek')
        self.assertEqual(type(search.primary).__name__,'DeepSeekSearcher')

    def test_school_and_name_anchored_global_fallback(self):
        calls=[]
        def search(q,site=None):calls.append((q,site));return []
        collect_for_claim(claim('学历'),'合成测试甲',SimpleNamespace(search=search),object(),fetcher=SimpleNamespace(get=lambda u:None),
                          queries=[Query('"合成测试甲"',site='school')],budget=Budget(max_searches=3,max_page_reads=1))
        self.assertTrue(any(site is None and '清华大学' in q and '合成测试甲' in q for q,site in calls))

    def test_broader_search_does_not_accept_another_school(self):
        llm=Mock()
        result=extract_evidence(claim('学历'),SearchHit('https://pku.edu.cn/record',title='北京大学本科名单'),
                                '北京大学本科名单 合成测试甲',llm,candidate_name='合成测试甲')
        self.assertIsNone(result);llm.complete_json.assert_not_called()


class MoneyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.env=patch.dict(os.environ,{'UNDERBOX_DB':str(Path(self.tmp.name)/'synthetic.db')});self.env.start();self.addCleanup(self.env.stop)

    def test_concurrent_reservations_cannot_overspend(self):
        ledger=CostBudget();count=[]
        def call(_):
            try:ledger.reserve(.05,'search',search=True);count.append(1)
            except BudgetLimit:pass
        with UsageExecutor(max_workers=8) as pool:list(pool.map(call,range(100)))
        self.assertEqual(len(count),70);self.assertLessEqual(ledger.snapshot()['estimatedCny'],4)

    def test_budget_persists_before_network_and_survives_reload(self):
        ledger=CostBudget();ledger.reserve(.05,'search',search=True)
        path=CostBudget.path(ledger.id);self.assertEqual(path.stat().st_mode&0o777,0o600)
        restored=CostBudget.load(ledger.id);self.assertEqual(restored.snapshot(),ledger.snapshot())

    def test_model_is_settled_from_returned_usage(self):
        ledger=CostBudget();body={'model':'deepseek-flash','max_tokens':4096,'messages':[{'role':'user','content':'合成文字'}]}
        response=Mock(status_code=200);response.json.return_value={'usage':{'prompt_tokens':100,'completion_tokens':50}}
        def post(*a,**k):
            self.assertGreater(ledger.spent,.53)
            return response
        with bind(ledger),patch('httpx.post',side_effect=post):DeepSeekClient('synthetic').raw_model_post('https://example.invalid',body,1)
        self.assertAlmostEqual(ledger.spent,.5006,places=6)

    def test_insufficient_money_blocks_model_before_http(self):
        ledger=CostBudget();ledger.reserve(3.49,'prior')
        body={'model':'deepseek-flash','max_tokens':4096,'messages':[]}
        with bind(ledger),patch('httpx.post') as network:
            self.assertRaises(BudgetLimit,DeepSeekClient('synthetic').raw_model_post,'https://example.invalid',body,1)
        network.assert_not_called();self.assertTrue(ledger.stopped)

    def test_unknown_model_price_fails_closed(self):
        ledger=CostBudget()
        with bind(ledger):self.assertRaises(BudgetLimit,reserve_model,'unpriced',{'model':'synthetic-unknown'})
        self.assertTrue(ledger.stopped)
        from app.llm import build_llm
        other=CostBudget()
        with bind(other),patch.dict(os.environ,{'DEEPSEEK_API_KEY':'synthetic','LLM_ENDPOINT':'https://unpriced.example/model'}),patch('httpx.post') as network:
            self.assertRaises(BudgetLimit,build_llm().complete_json,'json','synthetic')
        network.assert_not_called();self.assertIn('自定义模型端点',other.reason)

    def test_empty_deepseek_search_counts_real_requests_and_cost(self):
        ledger=CostBudget();response=Mock(status_code=200);response.json.return_value={'results':[]};base=METER.mark()
        with bind(ledger),patch('httpx.post',return_value=response):self.assertEqual(DeepSeekClient('synthetic').search('公开接口测试'),[])
        self.assertAlmostEqual(ledger.spent,.505);self.assertEqual(ledger.search_calls,1)
        self.assertEqual(METER.since(base)['deepseek']['tools']['search'],1)

    def test_search_budget_returns_partial_results_without_new_http(self):
        ledger=CostBudget();ledger.reserve(3.5,'prior')
        with bind(ledger),patch('httpx.post') as network:
            self.assertEqual(ZhipuSearcher(api_key='synthetic').search('q',site='school.example'),[])
        network.assert_not_called();self.assertTrue(ledger.stopped)

    def test_soft_target_stops_optional_index_only(self):
        ledger=CostBudget();ledger.reserve(2.5,'prior')
        primary=Mock();primary.search.return_value=[];secondary=Mock()
        with bind(ledger):self.assertEqual(RecallFallbackSearcher(primary,secondary).search('q'),[])
        primary.search.assert_called_once();secondary.search.assert_not_called()

    def test_new_report_budgets_are_isolated(self):
        first,second=CostBudget(),CostBudget();first.reserve(3.5,'prior')
        with bind(second):ticket=reserve_model('deepseek',{'model':'deepseek-flash','messages':[],'max_tokens':4096})
        self.assertIsNotNone(ticket);self.assertFalse(second.stopped);self.assertNotEqual(first.id,second.id)

    def test_no_usage_keeps_conservative_model_reservation(self):
        ledger=CostBudget();response=Mock(status_code=200);response.json.return_value={}
        with bind(ledger),patch('httpx.post',return_value=response):DeepSeekClient('synthetic').raw_model_post('https://example.invalid',{'model':'deepseek-flash','messages':[],'max_tokens':4096},1)
        self.assertGreater(ledger.spent,.53)

    def test_safety_counter_includes_each_search_request(self):
        ledger=CostBudget()
        self.assertEqual(ledger.search_limit,1000);ledger.search_limit=3
        for _ in range(3):ledger.reserve(.005,'cheap_search',search=True)
        self.assertRaises(BudgetLimit,ledger.reserve,.005,'cheap_search',search=True)
        self.assertEqual(ledger.search_calls,3);self.assertLess(ledger.spent,4)

    def test_transport_retry_retains_uncertain_previous_reservation(self):
        import httpx
        ledger=CostBudget();response=Mock(status_code=200);response.json.return_value={'results':[]}
        with bind(ledger),patch('httpx.post',side_effect=[httpx.ConnectError('synthetic'),response]),patch('time.sleep'):
            self.assertEqual(DeepSeekClient('synthetic').post('/v1/tools/search','search',retry=True,json={'query':'q'}),response)
        self.assertEqual(ledger.search_calls,2);self.assertAlmostEqual(ledger.spent,.51)

    def test_unpriced_search_provider_cannot_bypass_money_limit(self):
        ledger=CostBudget()
        with bind(ledger):self.assertRaises(BudgetLimit,build_searcher,'generic')
        self.assertTrue(ledger.stopped)

    def test_ocr_reserves_per_pdf_page(self):
        import fitz
        with fitz.open() as doc:
            doc.new_page();doc.new_page();data=doc.tobytes()
        ledger=CostBudget();response=Mock(status_code=200)
        response.json.return_value={'pages':[{'index':0,'finish_reason':'stop','text':'合成'},{'index':1,'finish_reason':'stop','text':'合成'}]}
        with bind(ledger),patch('httpx.post',return_value=response):DeepSeekClient('synthetic').ocr_bytes(data,'synthetic.pdf')
        self.assertAlmostEqual(ledger.spent,.503)

    def test_budget_stop_preserves_all_report_rows(self):
        from app.pipeline import PreparedResume,verify_prepared
        items=[claim().model_copy(update={'id':str(i)}) for i in range(2)]
        doc=SimpleNamespace(kind='text',source='synthetic.txt',risks=[])
        llm=Mock();llm.complete_json.return_value={'question':'请补充材料？'}
        prepared=PreparedResume(resume_path='synthetic.txt',doc=doc,llm=llm,profile=ResumeProfile(identity='student'),claims=items,name='合成测试甲',skipped=[])
        ledger=CostBudget()
        def collect(*args,**kwargs):ledger.stop('合成费用预算停止');return [],True
        with bind(ledger),patch('app.pipeline.employer_policy',return_value=[]),patch('app.pipeline.collect_for_claim',side_effect=collect),patch('app.contact_fallback.attach_contact_reviews'):
            report=verify_prepared(prepared,searcher=SimpleNamespace(search=lambda *a,**k:[]),fetcher=SimpleNamespace(get=lambda u:None))
        self.assertEqual(len(report.claims),2)
        self.assertTrue(all(any('费用预算限制' in note for note in item.source_notes) for item in report.claims))

    def test_school_discovery_threads_share_the_same_money_budget(self):
        from app.discover import discover
        from app.cost_budget import CURRENT
        ledger=CostBudget();seen=[]
        def fetch(url):
            seen.append(CURRENT.get() is ledger);CURRENT.get().reserve(.005,'synthetic_fetch');return None
        with bind(ledger),patch('app.discover._homepage',return_value=None),patch('app.discover._offices',return_value=['https://one.example/','https://two.example/']):
            discover('虚构学校','school.example','receiver',fetch,why=lambda u:'HTTP 404')
        self.assertEqual(seen,[True,True]);self.assertAlmostEqual(ledger.spent,.51)


if __name__=='__main__':unittest.main(verbosity=2)
