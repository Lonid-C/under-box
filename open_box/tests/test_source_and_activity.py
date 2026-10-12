"""回归：输入不能自证，活动不冒充项目，GitHub 入口按原文而非模型猜测。"""
import unittest
from unittest.mock import patch
from app.schema import Claim, Evidence
from app.homepage import same_page, HomepageSnapshot, to_document, read_homepage
from app.parse import ParsedDocument
from app.search import SearchHit
from app.collect import extract_evidence, _read_and_extract, collect_for_claim, Budget
from app.source_guard import SourceGuardFetcher, SourceGuardSearcher
from app.github_audit import bind_input, material_eligible, supplement_claims
from app.split import split_claims
from app.strategy import Query, ResumeProfile
from app.plan import plan_with_notes
from app import pipeline

SOURCE='https://www.example.edu.cn/person?id=7'
def claim(raw='参与 demo', **kw):
    return Claim(id='c1',category='项目',raw_text=raw,raw_locator='',date_label='',**kw)
class NoLLM:
    def complete_json(self,*a,**k): raise AssertionError('输入页面不应送去证据抽取')
class Fetcher:
    def __init__(self, redirect=None):self.calls=[];self.resolved_urls={};self.failures={};self.redirect=redirect
    def get(self,url):
        self.calls.append(url)
        if self.redirect:self.resolved_urls[url]=self.redirect
        return '<html><title>公开简介</title><body><p>'+'某候选人公开简介与学历经历。'*25+'</p></body></html>'
class SourceTests(unittest.TestCase):
    def test_variants_and_different_person_same_host(self):
        self.assertTrue(same_page(SOURCE,'http://example.edu.cn/person/?utm_source=glm&id=7#bio'))
        self.assertFalse(same_page(SOURCE,'https://example.edu.cn/person?id=8'))
        self.assertFalse(same_page(SOURCE,'https://example.edu.cn/news/7'))
        self.assertTrue(same_page('https://a.edu.cn/%E5%BC%A0?id=7&x=2','http://www.a.edu.cn/张?x=2&id=7'))
    def test_bnbu_facade_and_translation_aliases(self):
        self.assertTrue(same_page('https://www.bnbu.edu.cn/faculty.htm#/zhiqiangliao/','https://staff.bnbu.edu.cn/zhiqiangliao/cn'))
        self.assertTrue(same_page('https://staff.bnbu.edu.cn/zhiqiangliao/en','https://staff.bnbu.edu.cn/zhiqiangliao/cn'))
        self.assertFalse(same_page('https://staff.bnbu.edu.cn/other/cn','https://staff.bnbu.edu.cn/zhiqiangliao/cn'))
    def test_original_and_redirect_addresses_survive_parsing(self):
        final='https://faculty.example.edu.cn/p/7'
        snap=read_homepage(SOURCE,Fetcher(final))
        self.assertEqual(set(to_document(snap).source_urls),{SOURCE,final})
    def test_exclude_before_request_and_after_redirect(self):
        inner=Fetcher(SOURCE);guard=SourceGuardFetcher(inner,[SOURCE])
        self.assertIsNone(guard.get(SOURCE));self.assertEqual(inner.calls,[])
        self.assertIsNone(guard.get('https://other.edu.cn/redirect'));self.assertEqual(len(inner.calls),1)
        self.assertIsNone(guard.get_roster(SOURCE,claim()));self.assertEqual(len(inner.calls),1)
        other=Fetcher();self.assertIsNotNone(SourceGuardFetcher(other,[SOURCE]).get('https://example.edu.cn/news/7'))
    def test_search_content_and_snippet_both_excluded(self):
        class Search:
            def search(self,*a,**k):return [SearchHit(SOURCE,content='本人自述'),SearchHit(SOURCE+'#bio',snippet='本人自述'),SearchHit('https://example.edu.cn/news/7')]
        self.assertEqual([h.url for h in SourceGuardSearcher(Search(),[SOURCE]).search('姓名')],['https://example.edu.cn/news/7'])
    def test_extractor_and_read_batch_never_call_model_on_input(self):
        c=claim(entities={'excluded_source_urls':[SOURCE]});f=Fetcher()
        self.assertIsNone(extract_evidence(c,SearchHit(SOURCE),'本人自述',NoLLM()))
        ev=_read_and_extract(c,[SearchHit(SOURCE,content='本人自述')],f,NoLLM(),set())
        self.assertEqual(ev,[]);self.assertEqual(f.calls,[])
    def test_redirect_in_batch_never_extracts_or_falls_back_to_snippet(self):
        c=claim(entities={'excluded_source_urls':[SOURCE]})
        f=Fetcher(SOURCE)
        self.assertEqual(_read_and_extract(c,[SearchHit('https://other.edu.cn/r',snippet='候选人原文')],f,NoLLM(),set()),[])
    def test_provided_source_url_and_glm_cached_content_are_not_evidence(self):
        c=claim(entities={'excluded_source_urls':[SOURCE],'provided_urls':[SOURCE]})
        class Search:
            def search(self,*a,**k):return [SearchHit(SOURCE,title='候选人',content='候选人本人自述',snippet='候选人本人自述')]
        f=Fetcher();ev,_=collect_for_claim(c,'候选人',Search(),NoLLM(),fetcher=f,queries=[Query('候选人 demo')],budget=Budget(max_searches=1,max_page_reads=3))
        self.assertEqual(ev,[]);self.assertEqual(f.calls,[])
    def test_pipeline_defense_filters_source_and_reposts_without_blocking_school(self):
        doc=ParsedDocument('候选人的输入页面',[],SOURCE,'web');doc.source_urls=[SOURCE,'https://old.edu.cn/profile']
        c=claim(raw='参与公开测试软件项目',entities={'title':'公开测试软件项目'});prepared=pipeline.PreparedResume(SOURCE,doc,NoLLM(),ResumeProfile(name='候选人'),[c],'候选人')
        vals=[Evidence(title='公开材料',publisher='',source_tier='C',snippet='候选人参与公开测试软件项目',url=SOURCE),Evidence(title='公开材料',publisher='',source_tier='C',snippet='候选人参与公开测试软件项目',url='https://old.edu.cn/profile'),Evidence(title='公开材料',publisher='',source_tier='C',snippet='候选人参与公开测试软件项目',url='https://mirror.edu.cn/a',origin_url=SOURCE),Evidence(title='公开材料',publisher='',source_tier='C',snippet='候选人参与公开测试软件项目',url='https://example.edu.cn/news/7')]
        with patch.object(pipeline,'collect_for_claim',return_value=(vals,False)),patch.object(pipeline,'generate_question',return_value='补证'):
            report=pipeline.verify_prepared(prepared,searcher=object(),fetcher=object())
        self.assertEqual([e.url for e in report.claims[0].evidence],['https://example.edu.cn/news/7'])
class ActivityTests(unittest.TestCase):
    def split(self,raw,category='项目',entities=None):
        class LLM:
            def complete_json(self,*a,**k):return {'claims':[{'category':category,'raw_text':raw,'entities':entities or {},'elements':['项目名称=原文活动','角色=参与']} ]}
        return split_claims(raw,LLM())[0]
    def test_public_lesson_is_teaching(self):
        c=self.split('他所组织的徐洪武音钢琴公开课受到了各方好评，成为互联网上连载时间最久的公共课程之一。')
        self.assertEqual(c.category,'教学活动');self.assertFalse(material_eligible(c));self.assertEqual(c.elements[0],'活动名称=原文活动')
    def test_premiere_is_art(self):
        c=self.split('世界首演了Frederic Rzewski、高平等作曲家的作品。')
        self.assertEqual(c.category,'艺术成果');self.assertFalse(material_eligible(c))
        self.assertEqual(self.split('世界首演作曲家的作品 https://github.com/team/score').category,'艺术成果')
        self.assertTrue(plan_with_notes(c,'合成老师',ResumeProfile()).queries)
    def test_music_software_and_research_projects_stay_projects(self):
        for raw in ['开发钢琴音频识别算法系统','组织教学改革项目公开课，项目编号 A12','研发音乐会排期软件平台 https://github.com/a/demo']:
            self.assertEqual(self.split(raw).category,'项目')
    def test_allowed_categories_keep_artist_work_and_do_not_change_awards(self):
        self.assertEqual(self.split('世界首演钢琴作品','艺术成果').category,'艺术成果')
        self.assertEqual(self.split('钢琴演奏竞赛一等奖','竞赛').category,'竞赛')
    def test_github_gate_uses_literal_repository_mention(self):
        for raw in ['开发 demo','钢琴公开课','开发 demo，熟悉 GitHub','开发 demo，熟悉 GitHub 仓库管理','开发 demo https://github.com/alice','开源 demo 的 GitLab 仓库']:
            self.assertFalse(material_eligible(bind_input([claim(raw)],raw)[0]))
        for raw in ['开发 demo，源码在 GitHub 仓库','参与 demo https://github.com/team/demo','开发 demo，GitHub repository 待补充']:
            self.assertTrue(material_eligible(bind_input([claim(raw)],raw)[0]))
    def test_model_invented_github_entity_and_raw_cannot_enable_entry(self):
        c=claim('开发 demo',entities={'repo':'a/demo','github_material_eligible':True})
        self.assertFalse(material_eligible(bind_input([c],'开发 demo')[0]))
        c=claim('开发 demo https://github.com/a/demo')
        self.assertFalse(material_eligible(bind_input([c],'开发 demo')[0]))
    def test_global_account_not_applied_to_unrelated_projects(self):
        c=claim('参与 demo');text='GitHub: https://github.com/alice\n\n参与 demo'
        self.assertFalse(material_eligible(bind_input([c],text)[0]))
    def test_associated_repository_missing_from_raw_is_eligible(self):
        c=claim('参与 demo');text='参与 demo\n源码： https://github.com/team/demo'
        self.assertTrue(material_eligible(bind_input([c],text)[0]))
    def test_supplement_rejects_unmentioned_project(self):
        with self.assertRaises(ValueError):supplement_claims([claim()],{'c1':{'urls':['https://github.com/team/demo']}})
        c=bind_input([claim('demo 原文未提仓库')],'demo 原文未提仓库')[0]
        with self.assertRaises(ValueError):supplement_claims([c],{'c1':{'urls':['https://github.com/team/demo']}})
