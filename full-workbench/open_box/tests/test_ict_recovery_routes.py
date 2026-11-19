"""Synthetic search-plan, document-chain and transport recovery regressions."""
import copy
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from app.schema import Claim
from app.strategy import build_plan
from app.retrieval_recovery import clean_anchor, clean_query, identifiers, school_record_links, identifier_feedback
from app.search import PageFetcher, SearchHit


def claim(category='项目', raw='林示例在清华大学参加教学改革项目，项目编号：25Z-AB019', **entities):
    return Claim(id='synthetic', category=category, raw_text=raw, raw_locator='合成材料',
        date_label='2025', elements=[], entities={'candidate_name': '林示例', 'school': '清华大学', **entities})


class RecoveryRouteTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'UNDERBOX_ICT_RECALL':'1', 'UNDERBOX_ICT_UNLIMITED':'1',
                                      'UNDERBOX_ICT_ENABLE':'1', 'UNDERBOX_ICT_COST_BUDGET':'0'})
        env.start(); self.addCleanup(env.stop)

    def test_number_is_a_first_round_route_without_changing_claim(self):
        c=claim(); c.elements=['项目编号=25Z-AB019', '状态=结题', '经费=2万']
        before=copy.deepcopy(c.model_dump()); plan=build_plan(c,'林示例')
        q=next(q for q in plan.queries if q.source=='recovery:identifier')
        self.assertEqual(q.text,'"25Z-AB019"'); self.assertEqual(q.round,1)
        self.assertIn('不证明',q.purpose); self.assertEqual(c.model_dump(),before)
        self.assertEqual(plan.queries[0].source,'recovery:identifier')

    def test_model_invented_number_is_not_a_query_anchor(self):
        c=claim(raw='林示例参加教学改革项目',project_id='25Z-AB019')
        c.elements=['项目编号=25Z-AB019']
        self.assertEqual(identifiers(c),[])

    def test_phone_and_amount_are_not_project_identifiers(self):
        self.assertEqual(identifiers(claim(raw='林示例电话13800000000，经费25000元')),[])

    def test_project_and_group_have_person_and_official_roster_routes(self):
        c = claim(raw='林示例加入清华大学微纳系统实验室，参与示例微纳传感器项目。',
                  title='示例微纳传感器项目', research_group='微纳系统实验室')
        queries = build_plan(c, '林示例').queries
        self.assertCountEqual([q.text for q in queries if q.source == 'recovery:project_person'],
                              ['"林示例" "示例微纳传感器项目"', '"林示例" "微纳系统实验室"'])
        self.assertTrue(any(q.site == 'school' and q.source == 'recovery:project_notice'
                            and '示例微纳传感器项目' in q.text for q in queries))
        self.assertTrue(any(q.site == 'school' and q.source == 'recovery:project_roster'
                            and '微纳系统实验室' in q.text for q in queries))

    def test_project_group_found_in_existing_split_only_when_named(self):
        c = claim(raw='林示例加入清华大学微纳系统实验室。', research_group='凭空生成的实验室')
        self.assertTrue(any(q.source == 'recovery:project_person' and '微纳系统实验室' in q.text
                            for q in build_plan(c, '林示例').queries))
        c = claim(raw='林示例加入项目组，参加学校科研项目。', research_group='项目组', title='科研项目')
        self.assertFalse(any(q.source.startswith('recovery:project_')
                             for q in build_plan(c, '林示例').queries))

    def test_only_matching_official_group_page_follows_generic_member_link(self):
        c = claim(raw='林示例加入清华大学微纳系统实验室。', research_group='微纳系统实验室')
        page = SearchHit('https://lab.tsinghua.edu.cn/overview', title='简介')
        html = ('<html><h1>微纳系统实验室</h1><a href="/members">课题组成员</a>'
                '<a href="/members.pdf">团队成员</a><a href="/old">2024 年成员名单</a>'
                '<a href="https://example.org/roster">团队成员</a><a href="/index">首页</a></html>')
        self.assertEqual([hit.url for hit in school_record_links(html, page, c)],
                         ['https://lab.tsinghua.edu.cn/members', 'https://lab.tsinghua.edu.cn/members.pdf'])
        self.assertEqual(school_record_links(html.replace('微纳系统实验室', '其他实验室'), page, c), [])

    def test_patent_application_number_is_not_used_as_guessed_publication_url(self):
        c=claim('专利','林示例，一种合成测试方法，CN202510123456.7，申请公开')
        qs=build_plan(c,'林示例').queries
        self.assertTrue(any(q.text=='"202510123456.7" 申请 专利' for q in qs))
        self.assertFalse(any('/patent/CN202510123456' in q.text for q in qs))

    def test_query_commentary_and_nested_quotes_removed_only_from_queries(self):
        self.assertEqual(clean_anchor('示例大学（据本科就读推断，原文未明写）'),'示例大学')
        self.assertEqual(clean_anchor('示例大学（深圳）'),'示例大学（深圳）')
        self.assertEqual(clean_query('"示例大学" "示例大学" "“示例奖”"'),'"示例大学" "示例奖"')

    def test_long_paper_uses_short_anchor_and_full_name_for_discovery(self):
        title='PXNet & QNet: Two Synthetic Reconstruction Architectures by Transforming Images in Multiple Scenarios'
        c=claim('论文','Lin S., Chen X. '+title+'. Synthetic Journal 2023.',title=title,venue='Synthetic Journal')
        before=copy.deepcopy(c.model_dump()); qs=build_plan(c,'林示例').queries
        self.assertTrue(any(q.source=='recovery:title_keywords' and q.text=='"PXNet" "QNet"' for q in qs))
        self.assertTrue(any(q.source=='recovery:author_alias' and '"shili lin"' in q.text.lower() for q in qs))
        self.assertEqual(c.model_dump(),before)

    def test_invented_title_has_no_short_title_recovery(self):
        c=claim('论文','林示例发表研究成果',title='PXNet: An Invented Long Paper Title for Testing')
        self.assertFalse(any(q.source.startswith('recovery:title') for q in build_plan(c,'林示例').queries))

    def test_discovery_does_not_relax_exact_title_or_author_identity(self):
        from app.paper_identity import from_hit
        c=claim('论文','Lin S., Chen X. PXNet & QNet: Synthetic Reconstruction. 2023.',title='PXNet & QNet: Synthetic Reconstruction')
        hit=SearchHit('https://doi.org/10.5555/synthetic',title='PXNet and QNet: Synthetic Reconstruction',
            year='2024',author_records=({'name':'Shili Lin','institutions':['清华大学'],'position':1},),
            registry_sources=('https://api.crossref.org/works/10.5555/synthetic',))
        self.assertIsNone(from_hit(c,hit))

    def test_competition_can_find_pdf_index_without_unreliable_edition_in_query(self):
        c=claim('竞赛','林示例在清华大学参加2025年第十届示例竞赛获二等奖',contest='示例竞赛')
        qs=build_plan(c,'林示例').queries
        self.assertTrue(any(q.text=='"林示例" "清华大学"' and q.source=='recovery:person_school' for q in qs))
        self.assertIn('第十届',c.raw_text)

    def test_award_recovery_remains_award_specific(self):
        c=claim('奖学金','林示例获2025年清华大学示例奖学金',award='示例奖学金')
        qs=build_plan(c,'林示例').queries
        self.assertTrue(any(q.source=='recovery:award_roster' for q in qs))
        self.assertTrue(all('奖学金' in q.text or '荣誉' in q.text or '表彰' in q.text for q in qs))

    def test_repeated_routes_are_deduplicated(self):
        c=claim(); c.elements=['项目编号=25Z-AB019']; qs=build_plan(c,'林示例').queries
        self.assertEqual(len(qs),len({(q.text,q.site,q.kind) for q in qs}))

    def test_fuzzy_identifier_results_trigger_topic_and_funding_search(self):
        from app.strategy import Query
        c=claim();q=Query('"25Z-AB019"',source='recovery:identifier')
        hits=[SearchHit('https://other.example/wrong',title='Contract 25Z-AB0199')]
        matched,follow=identifier_feedback(c,q,hits)
        self.assertEqual(matched,[]);self.assertEqual(len(follow),2)
        self.assertIn('教学改革',follow[0].text);self.assertIn('grant funding',follow[1].text)
        self.assertEqual(identifier_feedback(c,follow[0],hits),(hits,[]))

    def test_exact_identifier_match_does_not_launch_extra_queries(self):
        from app.strategy import Query
        q=Query('"25Z-AB019"',source='recovery:identifier')
        hits=[SearchHit('https://public.example/grant',snippet='项目编号：25Z-AB019。立项公示')]
        self.assertEqual(identifier_feedback(claim(),q,hits),(hits,[]))

    def test_identifier_feedback_is_executed_and_fuzzy_page_not_read(self):
        from app.collect import collect_for_claim, Budget
        from app.strategy import Query
        query=Query('"25Z-AB019"',source='recovery:identifier')
        search=SimpleNamespace(search=Mock(side_effect=lambda text,site=None: [SearchHit('https://other.example/wrong',title='25Z-AB0199')] if text==query.text else []))
        fetcher=SimpleNamespace(get=Mock(return_value=None));logs=[]
        collect_for_claim(claim(),'林示例',search,Mock(),queries=[query],fetcher=fetcher,budget=Budget(),progress=logs.append)
        self.assertIn('"25Z-AB019" 教学改革',[call.args[0] for call in search.search.call_args_list])
        self.assertIn('"25Z-AB019" grant funding',[call.args[0] for call in search.search.call_args_list])
        self.assertTrue(any('剔除模糊结果' in s for s in logs))
        fetcher.get.assert_not_called()

    def test_legacy_plan_does_not_receive_ict_routes(self):
        with patch.dict(os.environ,{'UNDERBOX_ICT_RECALL':'0'}):
            self.assertFalse(any(q.source.startswith('recovery:') for q in build_plan(claim(),'林示例').queries))

    def test_official_award_notice_follows_attachment_and_rejects_other_year_and_domain(self):
        c=claim('奖学金','2025年清华大学示例奖学金',award='示例奖学金')
        page=SearchHit('https://www.tsinghua.edu.cn/notice',title='2025示例奖学金公示')
        html='<html><a href="/list.pdf">附件：获奖名单</a><a href="/old.pdf">2024年名单</a><a href="https://other.example/list.pdf">附件</a><a href="/home">首页</a></html>'
        links=school_record_links(html,page,c)
        self.assertEqual([p.url for p in links],['https://www.tsinghua.edu.cn/list.pdf'])

    def test_unrelated_official_notice_is_not_an_attachment_route(self):
        c=claim('奖学金','2025年清华大学示例奖学金',award='示例奖学金')
        self.assertEqual(school_record_links('<html><a href="/list.pdf">附件</a></html>',SearchHit('https://tsinghua.edu.cn/a',title='采购公告'),c),[])

    def test_project_notice_attachment_is_actually_read_before_extraction(self):
        from app.collect import _read_and_extract, Budget
        c=claim(title='教学改革项目'); fetcher=SimpleNamespace(get=Mock(return_value='清华大学 | 林示例 | 项目编号25Z-AB019 | 已立项'))
        page=SearchHit('https://www.tsinghua.edu.cn/notice',title='2025教学改革项目立项公示')
        selected=[page]; budget=Budget(); budget.note_read()
        # First fetch is the notice; the next fetch is its linked roster.
        fetcher.get.side_effect=['<html><a href="/roster.doc">附件：立项名单</a></html>', '清华大学 | 林示例 | 25Z-AB019 | 已立项']
        with patch('app.collect.extract_evidence',return_value=None) as extract, patch('app.search.robots_allows',return_value=True):
            result=_read_and_extract(c,selected,fetcher,Mock(),set(),candidate_name='林示例',budget=budget)
        self.assertEqual(result,[]); self.assertEqual(fetcher.get.call_count,2)
        self.assertEqual(extract.call_count,1); self.assertIn('林示例',extract.call_args.args[2])
        self.assertEqual(extract.call_args.args[1].url,'https://www.tsinghua.edu.cn/roster.doc')

    def test_employer_filter_applies_to_new_attachment_route(self):
        from app.collect import _read_and_extract,Budget
        c=claim(title='教学改革项目',excluded_employer_sources={'domains':['tsinghua.edu.cn']})
        fetcher=SimpleNamespace(get=Mock())
        with patch('app.collect.extract_evidence') as extract:
            self.assertEqual(_read_and_extract(c,[SearchHit('https://tsinghua.edu.cn/n',title='教学改革项目')],fetcher,Mock(),set(),budget=Budget()),[])
        fetcher.get.assert_not_called();extract.assert_not_called()

    def test_full_latin_name_can_follow_registry_publisher_without_proving_identity(self):
        from app.collect import collect_for_claim, Budget
        from app.strategy import Query
        title='A Synthetic Reconstruction Study'
        c=claim('论文','Lin S., Chen X. '+title+'. Synthetic Journal.',title=title,venue='Synthetic Journal')
        hit=SearchHit('https://doi.org/10.5555/synthetic',title=title,authors=('Shili Lin','Xiang Chen'),
            official_url='https://publisher.example.net/article',content='registry metadata')
        fetcher=SimpleNamespace(get=Mock(return_value='publisher record'))
        search=SimpleNamespace(search=Mock(return_value=[]))
        with patch('app.collect.paper_hits',return_value=[hit]),patch('app.collect.extract_evidence',return_value=None),patch('app.search.robots_allows',return_value=True):
            evidence,_=collect_for_claim(c,'林示例',search,Mock(),queries=[Query(title,kind='crossref')],fetcher=fetcher,budget=Budget())
        self.assertIn('https://publisher.example.net/article',[call.args[0] for call in fetcher.get.call_args_list])
        self.assertEqual(evidence,[])

    def test_configured_doc_reader_is_isolated_to_ict_and_uses_bounded_parser(self):
        from app.public_documents import legacy_office_text
        data=b'\xd0\xcf\x11\xe0'+b'synthetic-not-a-real-resume'
        def run(command,**kwargs):
            self.assertIn('/synthetic/ict/antiword',command)
            kwargs['stdout'].write('合成公开名单'.encode())
            return SimpleNamespace(returncode=0)
        with patch.dict(os.environ,{'UNDERBOX_ICT_DOC_READER':'/synthetic/ict/antiword'}),patch('app.public_documents.subprocess.run',side_effect=run),patch('app.public_documents.shutil.which',return_value=None):
            self.assertEqual(legacy_office_text(data),'合成公开名单')
            with patch.dict(os.environ,{'UNDERBOX_ICT_RECALL':'0'}):self.assertIsNone(legacy_office_text(data))

    def test_transient_origin_block_expires_and_http_is_not_poisoned(self):
        f=PageFetcher()
        with patch('app.search.time.monotonic',return_value=100): f._mark_transient_failure('https://public.example/a','连接失败')
        with patch('app.search.time.monotonic',return_value=110):
            self.assertEqual(f._dead_reason('https://public.example/b'),'连接失败')
            self.assertEqual(f._dead_reason('http://public.example/b'),'')
            self.assertEqual(f._dead_reason('https://public.example:8443/b'),'')
        with patch('app.search.time.monotonic',return_value=131): self.assertEqual(f._dead_reason('https://public.example/b'),'')

    def test_connection_failure_can_fetch_later_record_after_backoff(self):
        f=PageFetcher();url='https://public.example/a'
        response=httpx.Response(200,content=b'<html>public record</html>',request=httpx.Request('GET',url))
        with patch('app.search.robots_allows',return_value=True), patch.object(f,'_throttle'), patch('app.search.time.sleep'), patch('app.safe_http.public_request',side_effect=httpx.ConnectTimeout('synthetic')) as network:
            self.assertIsNone(f.get(url));self.assertEqual(network.call_count,2)
        deadline=f._dead_until['https://public.example']
        with patch('app.search.time.monotonic',return_value=deadline+1), patch('app.search.robots_allows',return_value=True), patch.object(f,'_throttle'), patch('app.safe_http.public_request',return_value=response) as network:
            self.assertIn('public record',f.get('https://public.example/b'));network.assert_called_once()

    def test_ssl_failure_stays_strict_and_does_not_disable_verification(self):
        f=PageFetcher()
        with patch('app.search.robots_allows',return_value=True), patch.object(f,'_throttle'), patch('app.safe_http.public_request',side_effect=httpx.ConnectError('SSL CERTIFICATE expired')) as network:
            self.assertIsNone(f.get('https://public.example/a'))
            self.assertIsNone(f.get('https://public.example/b'))
        network.assert_called_once();self.assertIn('证书',f.failures['https://public.example/a'])
