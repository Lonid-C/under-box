"""Historical-query improvements with synthetic inputs and mocked transports."""
import copy,os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
from concurrent.futures import ThreadPoolExecutor
from app.cost_budget import CostBudget,ClaimShare,ClaimAllocations,BudgetLimit,bind,search_allowed
from app.schema import Claim,VerifiedClaim,Report
from app.strategy import build_plan,ResumeProfile,Plan
from app.collect import Budget,_relevance
from app.search import SearchHit,ZhipuSearcher
from app.data_sources import AgentRun
from app.verification_agents import agent_for

def item(i=1,category='奖学金',award='校特等奖学金'):
    return Claim(id='synthetic-'+str(i),category=category,raw_text='合成测试甲 2023 清华大学 '+award,
                 raw_locator='合成摘录',date_label='2023',elements=['奖项='+award,'学校=清华大学'],
                 entities={'org':'清华大学','school':'清华大学','award':award})

class OptimizationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        env=patch.dict(os.environ,{'UNDERBOX_ICT_RECALL':'1','UNDERBOX_ENV_FILE':'/dev/null',
            'UNDERBOX_DB':str(Path(self.temp.name)/'users.db'),'CLAIM_WORKERS':'3','COLLECT_WORKERS':'1'})
        env.start();self.addCleanup(env.stop)

    def test_early_claim_cannot_spend_later_claims_funds(self):
        ledger=CostBudget();pool=ClaimAllocations(ledger,['a','b','c'])
        with pool.claim('a') as share:
            self.assertRaises(BudgetLimit,share.reserve,2,'synthetic')
            self.assertTrue(share.stopped);self.assertFalse(ledger.stopped)
        with pool.claim('b') as share:share.reserve(.3,'synthetic')
        with pool.claim('c') as share:share.reserve(.3,'synthetic')
        self.assertAlmostEqual(CostBudget.load(ledger.id).spent,1.1)

    def test_unused_allowance_is_reallocated(self):
        ledger=CostBudget();pool=ClaimAllocations(ledger,['a','b'])
        with pool.claim('a') as first:initial=first.limit
        with pool.claim('b') as second:self.assertGreater(second.limit,initial*1.9)

    def test_parallel_allocations_preserve_every_pending_claim(self):
        ledger=CostBudget();pool=ClaimAllocations(ledger,[str(i) for i in range(15)])
        def work(i):
            with pool.claim(str(i)) as share:
                share.reserve(.05,'synthetic_search',search=True)
                share.reserve(.05,'synthetic_search',search=True)
                return share.limit
        with ThreadPoolExecutor(max_workers=5) as workers:limits=list(workers.map(work,range(15)))
        self.assertEqual(len(limits),15);self.assertEqual(ledger.search_calls,30)
        self.assertAlmostEqual(ledger.spent,2.0);self.assertFalse(ledger.stopped)

    def test_search_share_leaves_money_for_extraction(self):
        ledger=CostBudget();share=ClaimShare(ledger,.2)
        with bind(share):
            share.reserve(.1,'synthetic_search',search=True)
            self.assertRaises(BudgetLimit,share.reserve,.05,'synthetic_search',search=True)
            self.assertFalse(search_allowed());self.assertFalse(share.stopped)
            reservation=share.reserve(.08,'synthetic_model')
            share.settle(reservation,.03)
        self.assertAlmostEqual(share.spent,.13);self.assertAlmostEqual(ledger.spent,.63)

    def test_retry_requests_cannot_bypass_claim_share(self):
        ledger=CostBudget();share=ClaimShare(ledger,.04)
        response=Mock(status_code=503);response.raise_for_status.side_effect=ValueError('synthetic network error')
        with bind(share),patch('httpx.post',return_value=response) as network,patch('app.search.time.sleep'),patch.object(ZhipuSearcher,'_throttle'):
            self.assertEqual(ZhipuSearcher(api_key='synthetic').search('合成查询',site='example.org'),[])
        self.assertEqual(network.call_count,2);self.assertEqual(ledger.search_calls,2)
        self.assertAlmostEqual(ledger.spent,.52);self.assertFalse(ledger.stopped)

    def test_honor_query_does_not_change_summer_award_to_merit_student(self):
        c=item(category='校内荣誉',award='夏令营优秀学员');before=copy.deepcopy(c.model_dump())
        plan=build_plan(c,'合成测试甲',ResumeProfile(identity='student'))
        self.assertTrue(any('夏令营' in q.text for q in plan.queries))
        self.assertFalse(any('三好学生' in q.text or '优秀学生' in q.text for q in plan.queries))
        self.assertEqual(c.model_dump(),before)

    def test_special_scholarship_keeps_original_award_and_official_roster(self):
        qs=build_plan(item(),'合成测试甲',ResumeProfile(identity='student')).queries
        self.assertFalse(any('国家奖学金' in q.text or '优秀学生' in q.text for q in qs))
        self.assertTrue(any(q.site in ('school','tsinghua.edu.cn') and '校特等奖学金' in q.text and '2023' in q.text and '名单' in q.text for q in qs))

    def test_explicit_national_award_is_still_queried(self):
        qs=build_plan(item(award='国家奖学金'),'合成测试甲',ResumeProfile(identity='student')).queries
        self.assertTrue(any('国家奖学金' in q.text for q in qs))

    def test_policy_off_preserves_original_query_templates(self):
        with patch.dict(os.environ,{'UNDERBOX_ICT_RECALL':'0'}):
            qs=build_plan(item(),'合成测试甲',ResumeProfile(identity='student')).queries
        self.assertTrue(any('国家奖学金' in q.text for q in qs))

    def test_original_read_scale_and_relevant_roster_priority(self):
        c=item();plan=build_plan(c,'合成测试甲',ResumeProfile(identity='student'))
        self.assertEqual(plan.budget['reads'],10)
        roster=SearchHit('https://tsinghua.edu.cn/award','2023 校特等奖学金获奖名单')
        unrelated=SearchHit('https://baike.example/award','国家奖学金介绍')
        self.assertGreater(_relevance(roster,'合成测试甲',c),_relevance(unrelated,'合成测试甲',c))

    def run_fifteen(self,ledger):
        from app.pipeline import PreparedResume,verify_prepared
        from app.llm import StubLLM
        categories=['学历']+['奖学金']*10+['竞赛','论文','论文','专利']
        claims=[item(i,category) for i,category in enumerate(categories,1)]
        prepared=PreparedResume('synthetic.txt',SimpleNamespace(kind='text',source='synthetic.txt',risks=[]),StubLLM([]),ResumeProfile(identity='student'),claims,'合成测试甲',[])
        def plan(c,*args):
            from app.plan import Query
            return Plan([Query(c.id+' 合成查询 '+str(n),site='example.org') for n in range(30)],'synthetic','合成策略',budget={'searches':96,'reads':10,'seconds':30})
        response=Mock(status_code=200);response.json.return_value={'search_result':[]}
        with bind(ledger),patch('app.pipeline.employer_policy',return_value=[]),patch('app.pipeline.plan_with_notes',side_effect=plan),patch('app.education_review.build_education_review',return_value={}),patch('app.contact_fallback.attach_contact_reviews'),patch('httpx.post',return_value=response) as network,patch.object(ZhipuSearcher,'_throttle'),patch('app.search.robots_allows',return_value=True):
            report=verify_prepared(prepared,searcher=ZhipuSearcher(api_key='synthetic'),fetcher=SimpleNamespace(get=lambda u:None))
        return report,network.call_count

    def test_full_pipeline_fifteen_claims_including_later_papers_get_searches(self):
        ledger=CostBudget();report,calls=self.run_fifteen(ledger)
        accesses=[c.verification_agent['sourceAccess'] for c in report.claims]
        self.assertEqual(len(accesses),15);self.assertTrue(all(a['budget']['searches']>0 for a in accesses))
        self.assertTrue(all(a['execution']['state']=='budget_limited' for a in accesses))
        self.assertTrue(all('costAllocation' in a for a in accesses))
        self.assertTrue(all(c.needs_human and '部分检索' in c.next_step for c in report.claims))
        self.assertEqual(calls,ledger.search_calls);self.assertLessEqual(ledger.spent,4)
        self.assertEqual([c.claim.id for c in report.claims],['synthetic-'+str(i) for i in range(1,16)])

    def test_zero_remaining_budget_is_not_reported_as_searched(self):
        ledger=CostBudget();ledger.reserve(3.5,'prior')
        report,calls=self.run_fifteen(ledger)
        self.assertEqual(calls,0)
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'underbox'))
        import serve
        output=serve.to_presentation(report)
        self.assertTrue(all(c['retrievalStatus']=='budget_not_started' for c in output['claims']))
        self.assertTrue(all('未执行检索' in c['nextStep'] for c in output['claims']))

    def test_execution_record_is_not_rewritten_by_later_global_stop(self):
        c=item();ledger=CostBudget();budget=Budget();budget.searches=1
        with bind(ledger):run=AgentRun(agent_for(c),c,build_plan(c,'合成测试甲'),budget)
        verified=VerifiedClaim(claim=c,status='none');run.finish(verified)
        ledger.stop('synthetic later stop')
        self.assertEqual(run.snapshot()['execution']['state'],'searched')

    def positive_pipeline(self,other_person=False):
        from app.pipeline import PreparedResume,verify_prepared
        from app.cost_budget import CURRENT
        from app.plan import Query
        c=item();person='合成另一人' if other_person else '合成测试甲'
        text='清华大学 2023 本科学生 '+person+' 获校特等奖学金。'
        class Model:
            def __init__(self):self.extractions=0
            def complete_json(self,system,user,**kwargs):
                if 'snippet' not in system:return {}
                self.extractions+=1;CURRENT.get().reserve(.01,'synthetic_extraction')
                return {'snippet':text,'supports':c.elements,'published_at':'2023-10-01','identity_signals':['学校一致']}
        model=Model();ledger=CostBudget()
        prepared=PreparedResume('synthetic.txt',SimpleNamespace(kind='text',source='synthetic.txt',risks=[]),model,ResumeProfile(identity='student'),[c],'合成测试甲',[])
        response=Mock(status_code=200);response.json.return_value={'search_result':[{'link':'https://www.tsinghua.edu.cn/synthetic-award','title':'2023 校特等奖学金获奖名单','content':text}]}
        plan=Plan([Query('合成奖项 '+str(i),site='tsinghua.edu.cn') for i in range(5)],'synthetic','合成策略',budget={'searches':8,'reads':10,'seconds':30})
        with bind(ledger),patch('app.pipeline.employer_policy',return_value=[]),patch('app.pipeline.plan_with_notes',return_value=plan),patch('app.contact_fallback.attach_contact_reviews'),patch('httpx.post',return_value=response),patch.object(ZhipuSearcher,'_throttle'),patch('app.search.robots_allows',return_value=True):
            report=verify_prepared(prepared,searcher=ZhipuSearcher(api_key='synthetic'),fetcher=SimpleNamespace(get=lambda u:text))
        return report,model,ledger

    def test_fair_budget_still_extracts_and_retains_valid_person_award(self):
        report,model,ledger=self.positive_pipeline()
        self.assertEqual(model.extractions,1);self.assertTrue(report.claims[0].evidence)
        self.assertIn('奖项=校特等奖学金',report.claims[0].proved)
        self.assertAlmostEqual(ledger.categories['synthetic_extraction'],.01)
        self.assertEqual(ledger.search_calls,5)

    def test_other_person_is_rejected_before_paid_extraction(self):
        report,model,ledger=self.positive_pipeline(other_person=True)
        self.assertEqual(model.extractions,0);self.assertFalse(report.claims[0].evidence)
        self.assertNotIn('synthetic_extraction',ledger.categories)

if __name__=='__main__':unittest.main()
