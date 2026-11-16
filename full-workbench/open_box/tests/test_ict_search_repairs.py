"""Synthetic fixtures; no paid providers or real candidate material."""
import copy,os,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from app.schema import Claim,VerifiedClaim
from app.strategy import build_plan,ResumeProfile
from app.competitions import find_competition,competition_for_claim,seed_pages
from app.source_guard import for_claim,policy,excluded,SourceGuardSearcher
from app.data_sources import AgentRun
from app.verification_agents import agent_for
from app.collect import Budget,extract_evidence,_read_and_extract
from app.search import SearchHit

def claim(category='竞赛',raw='2026 IMC Prosperity 4 全球量化交易挑战赛',**entities):
    entities.setdefault('candidate_identity_context',{'policy':'person_v2','name':'合成测试甲'})
    return Claim(id='synthetic',category=category,raw_text=raw,raw_locator='合成测试',
                 date_label='2026',elements=[],entities=entities)

def paper(**kwargs):
    return claim('论文','成果已被2026 Example Science Conference 收录',venue='Example Science Conference',year='2026',**kwargs)

def campus(schools=('清华大学',),hint='清华大学',raw='全国大学生电子商务三创赛校赛一等奖'):
    return claim(raw=raw,contest='三创赛',school_search_hint=hint,
                 candidate_identity_context={'education_schools':list(schools)})

SOURCES=policy(domains=['tsinghua.edu.cn','pku.edu.cn'],organizations=['清华大学','北京大学'])

class SearchRepairTests(unittest.TestCase):
    def test_prosperity_full_name_has_its_own_directory(self):
        self.assertEqual(find_competition('2026 IMC Prosperity 4')['id'],'imc-prosperity')

    def test_original_full_name_beats_abbreviated_model_entity(self):
        c=claim(contest='IMC')
        self.assertEqual(competition_for_claim(c)['id'],'imc-prosperity')
        plan=build_plan(c,'合成测试甲',ResumeProfile(identity='student'))
        self.assertTrue(any(q.site=='prosperity.imc.com' for q in plan.queries))
        self.assertFalse(any(q.site=='imc-math.org.uk' for q in plan.queries))
        from app.collect import collect_for_claim
        transport=Mock();transport.search_with_confirmation=None;transport.confirm_empty=None
        transport.search.return_value=[]
        with patch('app.collect.seed_pages',return_value=[]):
            collect_for_claim(c,'合成测试甲',transport,Mock(),queries=plan.queries,
                              fetcher=SimpleNamespace(get=lambda u:None),budget=Budget(max_searches=25,max_page_reads=1,max_seconds=10))
        self.assertFalse(any('"IMC" 官方网站' in call.args[0] for call in transport.search.call_args_list))
        self.assertFalse(any(call.kwargs.get('site')=='imc-math.org.uk' for call in transport.search.call_args_list))

    def test_math_contest_keeps_its_own_route(self):
        self.assertEqual(find_competition('International Mathematics Competition for University Students')['id'],'imc')
        self.assertIsNone(find_competition('IMMC'))

    def test_prosperity_never_seeds_math_rosters(self):
        c=claim(contest='IMC')
        self.assertTrue(all('prosperity.imc.com' in p['url'] for p in seed_pages(competition_for_claim(c),c)))

    def test_no_title_conference_has_grounded_author_probes(self):
        c=paper();before=copy.deepcopy(c.model_dump())
        qs=build_plan(c,'合成测试甲',ResumeProfile(identity='student')).queries
        self.assertGreaterEqual(len(qs),2)
        self.assertTrue(all('Example Science Conference' in q.text and '合成测试甲' in q.text and '2026' in q.text for q in qs))
        self.assertFalse(any(q.kind=='crossref' for q in qs))
        self.assertEqual(c.model_dump(),before)

    def test_existing_paper_title_keeps_title_based_strategy(self):
        qs=build_plan(paper(title='A Synthetic Paper'),'合成测试甲').queries
        self.assertTrue(qs)
        self.assertFalse(any(q.source.startswith('conference_author:') for q in qs))

    def test_invented_conference_field_does_not_trigger_fallback(self):
        c=claim('论文','2026 研究成果被收录',venue='Invented Conference')
        self.assertFalse(any(q.source.startswith('conference_author:') for q in build_plan(c,'合成测试甲').queries))

    def test_missing_author_does_not_make_a_generic_conference_probe(self):
        self.assertFalse(any(q.source.startswith('conference_author:') for q in build_plan(paper(),'').queries))

    def test_campus_hint_only_opens_matching_input_school(self):
        c=campus();before=copy.deepcopy(c.model_dump());scope=for_claim(c,SOURCES)
        self.assertFalse(excluded('https://news.tsinghua.edu.cn/campus',scope))
        self.assertTrue(excluded('https://news.pku.edu.cn/campus',scope))
        self.assertEqual(c.model_dump(),before)
        search=Mock();search.search.return_value=[]
        SourceGuardSearcher(search,scope).search('三创赛 校赛',site='tsinghua.edu.cn')
        self.assertNotIn('-site:tsinghua.edu.cn',search.search.call_args.args[0])

    def test_national_or_provincial_award_still_excludes_school(self):
        for raw in ['三创赛省赛二等奖，曾参加校赛','三创赛国赛一等奖（校赛晋级）']:
            self.assertTrue(excluded('https://tsinghua.edu.cn/',for_claim(campus(raw=raw),SOURCES)))

    def test_ambiguous_education_does_not_unlock_school_hint(self):
        self.assertTrue(excluded('https://tsinghua.edu.cn/',for_claim(campus(('清华大学','北京大学')),SOURCES)))

    def test_unrelated_hint_does_not_unlock_school(self):
        self.assertTrue(excluded('https://pku.edu.cn/',for_claim(campus(hint='北京大学'),SOURCES)))

    def test_missing_hint_does_not_guess_school(self):
        self.assertTrue(excluded('https://tsinghua.edu.cn/',for_claim(campus(schools=(),hint=''),SOURCES)))

    def test_undated_campus_award_uses_only_unique_input_school(self):
        c=campus(hint='');c.date_label='';before=copy.deepcopy(c.model_dump())
        self.assertFalse(excluded('https://tsinghua.edu.cn/',for_claim(c,SOURCES)))
        self.assertTrue(any(q.site in ('school','tsinghua.edu.cn') for q in build_plan(c,'合成测试甲').queries))
        self.assertEqual(c.model_dump(),before)

    def test_input_homepage_stays_excluded_even_for_campus_award(self):
        sources=policy(urls=['https://tsinghua.edu.cn/person'],domains=['tsinghua.edu.cn'],organizations=['清华大学'])
        self.assertTrue(excluded('https://tsinghua.edu.cn/person',for_claim(campus(),sources)))

    def run_scope(self,c):
        return AgentRun(agent_for(c),c,build_plan(c,'合成测试甲'),Budget())

    def test_missing_person_anchor_records_reason_without_model(self):
        c=claim('项目','合成测试甲开发项目');run=self.run_scope(c);model=Mock()
        with run.bind():
            got=extract_evidence(c,SearchHit('https://public.example/','背景'), '无关机构背景介绍',model,candidate_name='合成测试甲')
        self.assertIsNone(got);model.complete_json.assert_not_called()
        self.assertIn('正文缺少本人或团队身份锚点',run.snapshot()['decisionCounts'])

    def test_school_mismatch_is_diagnostic_not_a_weaker_evidence(self):
        c=claim('学历','清华大学本科',school='清华大学');run=self.run_scope(c)
        with run.bind():
            got=extract_evidence(c,SearchHit('https://pku.edu.cn/','北京大学'), '北京大学 合成测试甲 本科',Mock(),candidate_name='合成测试甲')
        self.assertIsNone(got);self.assertIn('学校不匹配',run.snapshot()['decisionCounts'])

    def test_model_error_is_recorded_without_exception_content(self):
        c=claim('项目','合成测试甲开发项目');run=self.run_scope(c);model=Mock()
        model.complete_json.side_effect=ValueError('secret-synthetic-error')
        with run.bind(),patch('app.collect.has_candidate_anchor',return_value=True):
            self.assertIsNone(extract_evidence(c,SearchHit('https://public.example/'),'合成测试甲',model,candidate_name='合成测试甲'))
        self.assertIn('模型调用或输出解析失败',run.snapshot()['decisionCounts'])
        self.assertNotIn('secret-synthetic-error',str(run.snapshot()))

    def test_diagnostic_log_is_bounded_and_does_not_displace_access_events(self):
        run=self.run_scope(paper())
        run.add({'action':'search','state':'returned'})
        for _ in range(130):run.decision('正文无本人锚点','https://example.com/?token=synthetic-secret')
        s=run.snapshot();self.assertEqual(len(s['decisions']),120);self.assertTrue(s['decisionsTruncated'])
        self.assertEqual(s['decisionCounts']['正文无本人锚点'],130);self.assertEqual(len(s['events']),1)
        self.assertNotIn('synthetic-secret',str(s));self.assertEqual(s['budget']['searches'],0)

    def test_excluded_result_is_logged_before_reading(self):
        c=claim('项目','合成测试甲开发项目');run=self.run_scope(c)
        transport=SimpleNamespace(search=lambda *a,**k:[SearchHit('https://tsinghua.edu.cn/private')])
        guarded=SourceGuardSearcher(transport,SOURCES)
        with run.bind():self.assertEqual(guarded.search('合成测试甲 项目'),[])
        self.assertIn('搜索结果来源处于排除范围',run.snapshot()['decisionCounts'])

    def test_no_query_pipeline_is_explicit_and_does_not_ask_model(self):
        from app.pipeline import PreparedResume,verify_prepared
        c=claim('论文','研究成果被收录');model=Mock()
        prepared=PreparedResume(resume_path='synthetic.txt',doc=SimpleNamespace(kind='text',source='synthetic.txt',risks=[]),llm=model,profile=ResumeProfile(identity='student'),claims=[c],name='合成测试甲',skipped=[])
        with patch('app.pipeline.employer_policy',return_value=[]),patch('app.contact_fallback.attach_contact_reviews'):
            report=verify_prepared(prepared,searcher=SimpleNamespace(search=Mock(return_value=[])),fetcher=SimpleNamespace(get=Mock(return_value=None)))
        vc=report.claims[0];self.assertTrue(vc.needs_human);self.assertIn('尚未执行',vc.next_step)
        self.assertEqual(vc.verification_agent['sourceAccess']['queryPlan']['state'],'no_queries')
        model.complete_json.assert_not_called()
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'underbox'))
        import serve
        self.assertEqual(serve.to_presentation(report)['claims'][0]['retrievalStatus'],'not_started')

if __name__=='__main__':unittest.main()
