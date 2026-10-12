"""现任机构全部渠道排除；保留独立来源，搜索/回读/判定各入口统一。"""
import unittest
from unittest.mock import patch

from app import pipeline, judge
from app.collect import Budget, collect_for_claim, extract_evidence, _read_and_extract
from app.employer_scope import current_employers, employer_policy
from app.identity import with_candidate_identity_context
from app.parse import ParsedDocument
from app.schema import Claim, Evidence
from app.search import SearchHit
from app.source_guard import (policy, excluded, evidence_excluded, SourceGuardSearcher,
                              SourceGuardFetcher, claim_sources, for_claim)
from app.strategy import Query, ResumeProfile

ORG='香港中文大学（深圳）'
SOURCE='https://music.cuhk.edu.cn/faculty/ni-fu'
TEXT='付妮，现任香港中文大学（深圳）音乐学院教授，毕业于中央音乐学院视唱练耳专业并留校任教。'
POLICY=policy([SOURCE],['cuhk.edu.cn'],[ORG])
OWN='https://diligentia.cuhk.edu.cn/taxonomy/term/241'
THIRD='https://publisher.example.org/books/7'

def c(raw='独立出版乐理教程、乐理教学视频', category='教学活动', **kw):
    return Claim(id=kw.pop('id','c1'),raw_text=raw,category=category,date_label=kw.pop('date_label',''),raw_locator='',**kw)

def ev(url=OWN,publisher=ORG,**kw):
    return Evidence(url=url,publisher=publisher,title='公开材料',snippet='付妮，现任香港中文大学（深圳）音乐学院教授，独立出版乐理教程、乐理教学视频。',source_tier='A',supports=['内容=独立出版乐理教程、乐理教学视频'],identity_score=99,**kw)

class NoLLM:
    def complete_json(self,*a,**k):raise AssertionError('被排除的渠道不能送模型抽取')

class Search:
    def __init__(self,hits=()):self.calls=[];self.hits=list(hits)
    def search(self,q,site=None):self.calls.append((q,site));return self.hits
    def confirm_empty(self,q,site=None):self.calls.append((q,site));return self.hits
    def search_with_confirmation(self,q,site=None,accept=None):
        self.calls.append((q,site));return [h for h in self.hits if accept is None or accept(h)]

class Fetch:
    def __init__(self,redirect=None):self.calls=[];self.resolved_urls={};self.redirect=redirect
    def get(self,url):
        self.calls.append(url)
        if self.redirect:self.resolved_urls[url]=self.redirect
        return TEXT
    def get_roster(self,url,claim):return self.get(url)

class EmployerSourceTests(unittest.TestCase):
    def test_current_school_from_full_input_even_if_split_omits_post(self):
        doc=ParsedDocument(TEXT,[],SOURCE,'web')
        s=Search();p=employer_policy([c()],doc,'付妮',s)
        self.assertEqual(p['organizations'],[ORG]);self.assertEqual(p['domains'],['cuhk.edu.cn'])
        self.assertEqual(s.calls,[])
        for url in [OWN,SOURCE,'https://www.cuhk.edu.cn/zh-hans/aggregate-search-teacher?page=11','https://music.cuhk.edu.cn/taxonomy/term/148?page=3']:
            self.assertTrue(excluded(url,p),url)

    def test_former_school_and_student_school_not_current_employer(self):
        for text in ['付妮曾任中央音乐学院教授。','付妮曾任职于中央音乐学院。','付妮目前在中央音乐学院就读本科。','付妮目前在中央音乐学院攻读博士学位。']:
            self.assertEqual(current_employers([],text,'付妮'),[],text)
        self.assertEqual(current_employers([],TEXT,'付妮'),[ORG])

    def test_former_and_current_jobs_same_sentence(self):
        self.assertEqual(current_employers([],'付妮曾任中央音乐学院教授，现任港中深教授。','付妮'),[ORG])

    def test_other_person_current_job_not_borrowed(self):
        self.assertEqual(current_employers([],'付妮毕业于中央音乐学院。导师王明现任北京大学教授。','付妮'),[])

    def test_unselected_dated_current_job_and_multiple_employers(self):
        job=c('香港中文大学（深圳）音乐学院教授','任职',date_label='2023-至今',entities={'org':ORG})
        second=c('现任北京大学兼职教授','任职',id='c2',entities={'org':'北京大学'})
        self.assertEqual(set(current_employers([job,second])),{ORG,'北京大学'})

    def test_model_invented_employer_not_added(self):
        self.assertEqual(current_employers([c('现任中央音乐学院教授','任职',entities={'org':ORG})]),['中央音乐学院'])

    def test_unknown_company_domain_discovery_has_no_person_name(self):
        job=c('现任合成技术有限公司工程师','任职',entities={'company':'合成技术有限公司'})
        s=Search([SearchHit(url='https://www.synthetic.example.com/',title='合成技术有限公司官方网站')])
        p=employer_policy([job],ParsedDocument(job.raw_text,[],'test.pdf','pdf'),'付妮',s)
        self.assertEqual(p['domains'],['example.com']);self.assertNotIn('付妮',s.calls[0][0])
        self.assertTrue(excluded('https://news.example.com/jobs',p))

    def test_domain_boundary_and_external_affiliation_mentions(self):
        self.assertFalse(excluded('https://cuhk.edu.cn.other.example/teacher',POLICY))
        self.assertFalse(excluded('https://www.ccom.edu.cn/news/7',POLICY))
        self.assertFalse(excluded(THIRD,POLICY,'人民音乐出版社'))
        self.assertFalse(evidence_excluded(ev(THIRD,'人民音乐出版社'),POLICY))

    def test_official_publisher_aliases_and_wechat(self):
        for pub in [ORG,ORG+'学勤书院',ORG+'音乐学院','港中深官方公众号']:
            self.assertTrue(excluded('https://mp.weixin.qq.com/s/7',POLICY,pub),pub)
        self.assertFalse(excluded(THIRD,POLICY,'香港中文大学（香港）'))

    def test_origin_domain_repost_is_excluded(self):
        self.assertTrue(evidence_excluded(ev(THIRD,'独立媒体',origin_url=OWN),POLICY))

    def test_search_skips_institution_site_without_provider_call(self):
        s=Search();g=SourceGuardSearcher(s,POLICY)
        self.assertEqual(g.search('付妮',site='music.cuhk.edu.cn'),[])
        self.assertEqual(g.search('付妮 site:cuhk.edu.cn'),[])
        self.assertEqual(s.calls,[])

    def test_open_search_negative_site_and_result_filters(self):
        s=Search([SearchHit(OWN),SearchHit(THIRD,publisher='人民音乐出版社'),SearchHit('https://mp.weixin.qq.com/s/7',publisher=ORG+'学勤书院')])
        hits=SourceGuardSearcher(s,POLICY).search('付妮 乐理教程')
        self.assertEqual([h.url for h in hits],[THIRD]);self.assertIn('-site:cuhk.edu.cn',s.calls[0][0])

    def test_confirmation_paths_cannot_bypass_guard(self):
        s=Search([SearchHit(OWN),SearchHit(THIRD)]);g=SourceGuardSearcher(s,POLICY)
        self.assertEqual(g.search_with_confirmation('付妮',site='cuhk.edu.cn'),[])
        self.assertEqual(g.confirm_empty('付妮',site='cuhk.edu.cn'),[])
        self.assertEqual(s.calls,[])
        self.assertEqual([h.url for h in g.search_with_confirmation('付妮',site='publisher.example.org')],[THIRD])
        self.assertEqual([h.url for h in g.confirm_empty('付妮',site='publisher.example.org')],[THIRD])

    def test_fetch_and_roster_blocked_before_read(self):
        f=Fetch();g=SourceGuardFetcher(f,POLICY)
        self.assertIsNone(g.get(OWN));self.assertIsNone(g.get_roster(OWN,c()));self.assertEqual(f.calls,[])

    def test_redirect_into_employer_cannot_be_extracted(self):
        f=Fetch(OWN);g=SourceGuardFetcher(f,POLICY)
        self.assertIsNone(g.get(THIRD))

    def test_extraction_pre_gate_blocks_cached_content(self):
        claim=c(entities={'excluded_employer_sources':POLICY})
        self.assertIsNone(extract_evidence(claim,SearchHit(OWN),TEXT,NoLLM()))
        self.assertIsNone(extract_evidence(claim,SearchHit(THIRD,publisher=ORG),TEXT,NoLLM()))

    def test_read_batch_does_not_fetch_current_school(self):
        claim=c(entities={'excluded_employer_sources':POLICY});f=Fetch()
        self.assertEqual(_read_and_extract(claim,[SearchHit(OWN)],f,NoLLM(),set()),[]);self.assertEqual(f.calls,[])

    def test_collector_skips_current_site_without_spending_budget(self):
        claim=c(entities={'excluded_employer_sources':POLICY,'provided_urls':[OWN]})
        s=Search([SearchHit(OWN,content=TEXT)]);f=Fetch();budget=Budget(max_searches=1,max_page_reads=3)
        result,_=collect_for_claim(claim,'付妮',s,NoLLM(),fetcher=f,budget=budget,
                                  queries=[Query('付妮',site='cuhk.edu.cn'),Query('付妮 乐理教程')])
        self.assertEqual(result,[]);self.assertEqual(f.calls,[]);self.assertEqual(len(s.calls),1)
        self.assertIn('-site:cuhk.edu.cn',s.calls[0][0])

    def test_judge_defense_with_forged_identity_score(self):
        claim=c(entities={'excluded_employer_sources':POLICY})
        self.assertEqual(judge.assess(claim,[ev()],rescore=False).evidence,[])
        self.assertEqual(judge.assess(claim,[ev(THIRD,'独立媒体',origin_url=OWN)],rescore=False).evidence,[])
        self.assertEqual(len(judge.assess(claim,[ev(THIRD,'人民音乐出版社')]).evidence),1)

    def test_institution_only_plan_moves_to_third_party_search(self):
        claim=c(entities={'excluded_employer_sources':POLICY});s=Search();budget=Budget(max_searches=1,max_page_reads=2)
        collect_for_claim(claim,'付妮',s,NoLLM(),fetcher=Fetch(),budget=budget,
                          queries=[Query('付妮 乐理教程 site:cuhk.edu.cn',site='cuhk.edu.cn')])
        self.assertEqual(len(s.calls),1);self.assertIsNone(s.calls[0][1])
        self.assertEqual(s.calls[0][0],'付妮 乐理教程 -site:cuhk.edu.cn')

    def test_pipeline_filters_school_without_employment_claim(self):
        doc=ParsedDocument(TEXT,[],SOURCE,'web')
        claim=c(elements=['内容=独立出版乐理教程、乐理教学视频'])
        prepared=pipeline.PreparedResume(SOURCE,doc,NoLLM(),ResumeProfile(name='付妮'),[claim],'付妮')
        with patch.object(pipeline,'collect_for_claim',return_value=([ev(),ev(THIRD,'人民音乐出版社')],False)) as collect,patch.object(pipeline,'generate_question',return_value='补证'):
            report=pipeline.verify_prepared(prepared,searcher=Search(),fetcher=Fetch())
        self.assertEqual([e.url for e in report.claims[0].evidence],[THIRD])
        self.assertIn(ORG,collect.call_args.args[0].entities['candidate_identity_context']['current_employers'])
        self.assertTrue(any('现任机构' in n for n in report.claims[0].source_notes))

    def test_pdf_and_unselected_employment_also_blocked(self):
        job=c('现任香港中文大学（深圳）音乐学院教授','任职',id='job',entities={'org':ORG})
        claim=c(elements=['内容=独立出版乐理教程、乐理教学视频'])
        doc=ParsedDocument(TEXT,[],'test.pdf','pdf');prepared=pipeline.PreparedResume('test.pdf',doc,NoLLM(),ResumeProfile(name='付妮'),[job,claim],'付妮')
        with patch.object(pipeline,'collect_for_claim',return_value=([ev()],False)) as collect,patch.object(pipeline,'generate_question',return_value='补证'):
            report=pipeline.verify_prepared(prepared,claim_ids=['c1'],searcher=Search(),fetcher=Fetch())
        self.assertEqual(report.claims[0].evidence,[])
        self.assertTrue(excluded(OWN,claim_sources(collect.call_args.args[0])))

    def test_policies_are_per_candidate(self):
        student=c('香港中文大学（深圳）音乐学院本科在读','学历',entities={'org':ORG})
        p=employer_policy([student],ParsedDocument(student.raw_text,[],'test.pdf','pdf'),'唐思',Search())
        self.assertFalse(excluded(OWN,for_claim(student,p)))
        self.assertTrue(excluded(OWN,for_claim(c(),p)))
        other=employer_policy([],ParsedDocument('其他候选人',[],'test.pdf','pdf'),'另一人',Search())
        self.assertFalse(excluded(OWN,other))

    def test_own_school_education_exception_keeps_input_page_excluded(self):
        degree=c('香港中文大学（深圳）音乐学院钢琴表演本科在读','学历',entities={'org':ORG})
        scope=for_claim(degree,POLICY)
        self.assertFalse(excluded(OWN,scope));self.assertTrue(excluded(SOURCE,scope))
        self.assertFalse(excluded(THIRD,scope,ORG))
        self.assertEqual(scope['allowed_academic_institutions'],[ORG])

    def test_current_school_cannot_prove_foreign_degree(self):
        degree=c('毕业于中央音乐学院视唱练耳专业','学历',entities={'org':'中央音乐学院'})
        self.assertTrue(excluded(OWN,for_claim(degree,POLICY)))
        hongkong=c('香港中文大学本科','学历',entities={'school':'香港中文大学'})
        self.assertTrue(excluded(OWN,for_claim(hongkong,POLICY)))

    def test_own_school_honors_allowed_but_other_experiences_not(self):
        for category in ['校内荣誉','奖学金']:
            honor=c('香港中文大学（深圳）优秀学生奖学金',category,entities={'org':ORG})
            self.assertFalse(excluded(OWN,for_claim(honor,POLICY)),category)
        for category in ['任职','教学活动','项目','论文','学生工作','艺术成果']:
            other=c('香港中文大学（深圳）举办乐理教程活动',category,entities={'org':ORG})
            self.assertTrue(excluded(OWN,for_claim(other,POLICY)),category)

    def test_national_award_not_automatically_own_school_honor(self):
        contest=c('香港中文大学（深圳）学生获得全国钢琴竞赛一等奖','竞赛',entities={'org':ORG})
        self.assertTrue(excluded(OWN,for_claim(contest,POLICY)))
        campus=c('香港中文大学（深圳）校级钢琴竞赛一等奖','竞赛',entities={'org':ORG})
        self.assertFalse(excluded(OWN,for_claim(campus,POLICY)))

    def test_exception_does_not_allow_another_current_employer(self):
        p=policy([SOURCE],['cuhk.edu.cn','pku.edu.cn'],[ORG,'北京大学'])
        p['domains_by_org']={ORG:['cuhk.edu.cn'],'北京大学':['pku.edu.cn']}
        degree=c('香港中文大学（深圳）本科','学历',entities={'org':ORG})
        scope=for_claim(degree,p)
        self.assertFalse(excluded(OWN,scope));self.assertTrue(excluded('https://pku.edu.cn/news',scope))

    def test_pdf_exception_metadata_and_provider_site(self):
        degree=c('香港中文大学（深圳）本科在读','学历',entities={'org':ORG})
        p=employer_policy([degree],ParsedDocument(degree.raw_text,[],'test.pdf','pdf'),'唐思',Search())
        scope=for_claim(degree,p);s=Search();g=SourceGuardSearcher(s,scope)
        g.search('唐思 本科',site='cuhk.edu.cn')
        self.assertEqual(s.calls,[('唐思 本科','cuhk.edu.cn')])
        self.assertEqual(scope['allowed_academic_institutions'],[ORG])

    def test_pipeline_applies_purpose_exception_to_each_claim(self):
        degree=c('香港中文大学（深圳）音乐学院本科在读','学历',id='degree',entities={'org':ORG})
        activity=c('举办钢琴公开课','教学活动',id='activity')
        doc=ParsedDocument(degree.raw_text,[],SOURCE,'web')
        prep=pipeline.PreparedResume(SOURCE,doc,NoLLM(),ResumeProfile(name='唐思'),[degree,activity],'唐思')
        def fake_collect(claim,name,searcher,llm,**kwargs):
            searcher.search('唐思 记录',site='cuhk.edu.cn')
            if claim.id=='degree':
                self.assertFalse(excluded(OWN,claim_sources(claim)))
            else:
                self.assertTrue(excluded(OWN,claim_sources(claim)))
            return [],False
        s=Search()
        with patch.object(pipeline,'collect_for_claim',side_effect=fake_collect),patch.object(pipeline,'generate_question',return_value='补证'):
            report=pipeline.verify_prepared(prep,searcher=s,fetcher=Fetch())
        self.assertEqual(len(s.calls),1)
        self.assertTrue(any('本条允许' in n for n in report.claims[0].source_notes))
