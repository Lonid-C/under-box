"""Synthetic regressions for table identity, work metadata and bounded readers."""
import os,time,unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from app.schema import Claim
from app.search import _pdf_text,PageFetcher,SearchHit,READ_DEADLINE
from app.identity import has_candidate_anchor
from app.person_scope import check_person
from app.paper_identity import from_hit,author_match,venue_matches
from app.strategy import build_plan,ResumeProfile
from app.split import correct_activity_category
from app.source_guard import for_claim,policy,excluded


def claim(category='竞赛',raw='林示例在清华大学参加2025校级程序设计竞赛，获得二等奖',**entities):
    ctx={'policy':'person_v2','candidate_name':'林示例','education_schools':['清华大学'],'current_employers':[]}
    return Claim(id='synthetic',category=category,raw_text=raw,raw_locator='合成材料',date_label='2025',
        elements=['奖级=二等奖'],entities={'candidate_name':'林示例','candidate_identity_context':ctx,**entities})


def pdf_roster(rows,section='本科组二等奖',id_header='序号'):
    import pymupdf
    doc=pymupdf.open();page=doc.new_page(width=800,height=400)
    page.insert_text((30,25),'2025 校级竞赛 '+section,fontname='china-s',fontsize=11)
    table=[[id_header,'学校','队员一','队员二','指导教师'],*rows]
    xs=[30,90,300,450,600,770];top=50;height=30
    for x in xs:page.draw_line((x,top),(x,top+height*len(table)))
    for n in range(len(table)+1):page.draw_line((xs[0],top+n*height),(xs[-1],top+n*height))
    for n,row in enumerate(table):
        for j,value in enumerate(row):page.insert_text((xs[j]+3,top+n*height+20),value,fontname='china-s',fontsize=10)
    data=doc.tobytes();doc.close();return data


class RetrievalRegressionTests(unittest.TestCase):
    def setUp(self):
        env=patch.dict(os.environ,{'UNDERBOX_ICT_RECALL':'1','UNDERBOX_ICT_COST_BUDGET':'0'})
        env.start();self.addCleanup(env.stop)

    def test_real_pdf_cells_restore_school_and_member_record(self):
        text=_pdf_text(pdf_roster([['1','清华大学','林示例','陈示例','周教练']]))
        self.assertIn('学校=清华大学 | 队员一=林示例',text)
        c=claim();self.assertTrue(has_candidate_anchor(c,'https://results.example/roster.pdf',text,'2025校级竞赛'))
        row=next(s for s in text.splitlines() if '学校=清华大学' in s)
        self.assertTrue(check_person(c,'https://results.example/roster.pdf',row,'2025校级竞赛').allowed)

    def test_real_pdf_cannot_borrow_school_from_previous_row(self):
        text=_pdf_text(pdf_roster([['1','清华大学','陈示例','周示例','周教练'],['2','北京大学','林示例','李示例','王教练']]))
        self.assertFalse(has_candidate_anchor(claim(),'https://results.example/roster.pdf',text,'2025校级竞赛'))

    def test_new_award_section_keeps_printed_grade_when_numbering_restarts(self):
        import pymupdf
        doc=pymupdf.open(stream=pdf_roster([['1','清华大学','陈示例','周示例','周教练']],'本科组一等奖'),filetype='pdf')
        second=pymupdf.open(stream=pdf_roster([['1','清华大学','林示例','李示例','王教练']],id_header='编号'),filetype='pdf')
        doc.insert_pdf(second);data=doc.tobytes();doc.close();second.close()
        text=_pdf_text(data);row=next(s for s in text.splitlines() if '学校=' in s and '林示例' in s)
        self.assertIn('原文名单分段=本科组二等奖',row);self.assertNotIn('一等奖',row)

    def test_school_in_footer_and_name_on_another_line_stays_unconfirmed(self):
        self.assertFalse(has_candidate_anchor(claim(),'https://results.example/list','清华大学\n名单\n林示例','2025校级竞赛'))

    def test_coach_cell_cannot_be_used_as_contestant_identity(self):
        text=_pdf_text(pdf_roster([['1','清华大学','李示例','陈示例','林示例']]))
        self.assertFalse(has_candidate_anchor(claim(),'https://results.example/roster.pdf',text,'2025校级竞赛'))

    def paper(self):
        title='Synthetic Imaging Reconstruction Methods'
        c=claim('论文',f'Example A., Test B. {title}. Synthetic Imaging Conference 2025 (SIC).',
                candidate_name='Alice Example',title=title,venue='Synthetic Imaging Conference 2025 (SIC)',
                candidate_identity_context={'policy':'person_v2','candidate_name':'Alice Example','education_schools':['清华大学']})
        c.elements=['论文标题='+title,'刊物=Synthetic Imaging Conference 2025 (SIC)','年份=2025']
        rows=({'name':'Alice Example','given':'Alice','family':'Example','position':1,'institutions':['清华大学']},
              {'name':'Bob Test','given':'Bob','family':'Test','position':2,'institutions':['北京大学']})
        hit=SearchHit('https://doi.org/10.5555/synthetic',title=title,venue='2025 IEEE Synthetic Imaging Conference Workshops (SICW)',
            year='2025',author_records=rows,publication_type='proceedings-article',
            registry_sources=('https://api.crossref.org/works/10.5555/synthetic',))
        return c,hit

    def test_venue_conflict_preserves_verified_title_and_year_only(self):
        from app import judge
        c,hit=self.paper();ev=from_hit(c,hit)
        self.assertIsNotNone(ev);self.assertIn(c.elements[0],ev.supports);self.assertIn('年份=2025',ev.supports)
        self.assertNotIn(c.elements[1],ev.supports);self.assertIn('Workshop',ev.snippet)
        result=judge.assess(c,[ev]);self.assertEqual(result.status,'part')
        self.assertNotIn(c.elements[1],result.proved);self.assertTrue(result.needs_human)

    def test_venue_formatting_alias_preserves_workshop_boundary(self):
        self.assertTrue(venue_matches('Synthetic Imaging Conference 2025 (SIC)','2025 IEEE Synthetic Imaging Conference'))
        self.assertFalse(venue_matches('Synthetic Imaging Conference (SIC)','Synthetic Imaging Conference Workshops (SICW)'))

    def test_chinese_author_joint_match_retains_partial_work_despite_venue_difference(self):
        from app.paper_identity import latin_names
        c,hit=self.paper();name='林示例';latin=latin_names(name)[0].title();given,family=latin.split()
        c.entities.update(candidate_name=name,candidate_identity_context={'policy':'person_v2','candidate_name':name,'education_schools':['清华大学']})
        c.raw_text=c.raw_text.replace('Example A.',family+' '+given[0]+'.')
        hit.author_records[0].update(name=latin,given=given,family=family)
        evidence=from_hit(c,hit);self.assertIsNotNone(evidence);self.assertIn('英文作者联合关联',evidence.identity_signals)
        self.assertNotIn(c.elements[1],evidence.supports)

    def test_chinese_pinyin_without_explicit_coauthor_is_still_rejected(self):
        from app.paper_identity import latin_names
        c,hit=self.paper();name='林示例';latin=latin_names(name)[0].title();given,family=latin.split()
        c.entities.update(candidate_name=name,candidate_identity_context={'policy':'person_v2','candidate_name':name,'education_schools':['清华大学']})
        c.raw_text=c.raw_text.replace('Example A., Test B.',family+' '+given[0]+'.')
        hit.author_records[0].update(name=latin,given=given,family=family)
        self.assertIsNone(from_hit(c,hit))

    def test_doi_mismatch_still_rejects_record(self):
        c,hit=self.paper();c.entities['doi']='10.5555/another'
        self.assertIsNone(from_hit(c,hit))

    def test_other_author_or_borrowed_coauthor_school_still_rejected(self):
        c,hit=self.paper();hit.author_records[0]['institutions']=['北京大学']
        hit.author_records[1]['institutions']=['清华大学']
        self.assertIsNone(from_hit(c,hit))

    def test_ict_fetch_factory_uses_native_reader_without_paid_fetch_first(self):
        from app.deepseek_fetch import build_fetcher,DeepSeekPageFetcher
        self.assertIs(type(build_fetcher()),PageFetcher)
        with patch.dict(os.environ,{'UNDERBOX_ICT_RECALL':'0'}):self.assertIsInstance(build_fetcher(),DeepSeekPageFetcher)

    def test_cached_failed_host_skips_lock_robots_and_network(self):
        fetcher=PageFetcher();fetcher.dead_hosts['failed.example']='连接失败'
        with patch.object(fetcher,'_host_lock',side_effect=AssertionError('must not wait')),patch('httpx.get') as network:
            self.assertIsNone(fetcher.get('https://failed.example/next'))
        network.assert_not_called();self.assertIn('已跳过',fetcher.failures['https://failed.example/next'])

    def test_expired_claim_deadline_starts_no_new_network_request(self):
        token=READ_DEADLINE.set(time.monotonic()-1)
        try:
            with patch('httpx.get') as network:self.assertIsNone(PageFetcher().get('https://public.example/page'))
            network.assert_not_called()
        finally:READ_DEADLINE.reset(token)

    def test_host_queue_wait_is_bounded_by_remaining_claim_time(self):
        fetcher=PageFetcher();lock=Mock();lock.acquire.return_value=False
        token=READ_DEADLINE.set(time.monotonic()+.2)
        try:
            with patch.object(fetcher,'_host_lock',return_value=lock),patch('httpx.get') as network:
                self.assertIsNone(fetcher.get('https://public.example/page'))
            self.assertGreater(lock.acquire.call_args.kwargs['timeout'],0)
            self.assertLessEqual(lock.acquire.call_args.kwargs['timeout'],.2);network.assert_not_called()
        finally:READ_DEADLINE.reset(token)

    def test_native_page_fetch_does_not_call_deepseek(self):
        import httpx
        from app.deepseek_fetch import build_fetcher
        response=httpx.Response(200,content=b'<html>public record</html>',request=httpx.Request('GET','https://public.example/page'))
        with patch('app.search.robots_allows',return_value=True),patch('app.safe_http.public_request',return_value=response),patch('app.deepseek_client.DeepSeekClient.fetch',side_effect=AssertionError('no paid fetch')):
            self.assertIn('public record',build_fetcher().get('https://public.example/page'))

    def test_exchange_is_not_an_award_and_has_real_source_agent(self):
        from app.verification_agents import agent_for
        c=claim('校内荣誉','2025年作为清华大学代表到示例研究院访问交流',org='示例研究院',school='清华大学')
        changed=correct_activity_category(c);self.assertEqual(changed.category,'交流活动');self.assertEqual(c.category,'校内荣誉')
        plan=agent_for(changed).refine_plan(changed,'林示例',build_plan(changed,'林示例',ResumeProfile(identity='student')))
        self.assertTrue(plan.queries);self.assertTrue(all('评奖' not in q.text and '荣誉' not in q.text for q in plan.queries))

    def test_exchange_with_actual_award_keeps_award_contract(self):
        c=claim('校内荣誉','2025年访问交流获得优秀学员',award='优秀学员')
        self.assertEqual(correct_activity_category(c).category,'校内荣誉')

    def test_exchange_does_not_open_actual_employer_source(self):
        c,s=self.project(employed=True);c.category='交流活动'
        self.assertTrue(excluded('https://www.tsinghua.edu.cn/visit',for_claim(c,s)))

    def test_compound_roles_are_queried_individually(self):
        c=claim('学生工作','林示例任清华大学科协赛事部部长、学生会办公室副部长',org='清华大学',role='赛事部部长、办公室副部长')
        c.elements=['职务=赛事部部长','职务=办公室副部长','学校=清华大学']
        queries=build_plan(c,'林示例',ResumeProfile(identity='student')).queries
        self.assertFalse(any('赛事部部长、办公室副部长' in q.text for q in queries))
        self.assertTrue(any('赛事部部长' in q.text for q in queries));self.assertTrue(any('办公室副部长' in q.text for q in queries))

    def test_student_wechat_queries_cover_roles_and_organization_rosters(self):
        from app.plan import WECHAT_HOST

        c = claim('学生工作', '林示例在清华大学担任校科协赛事部部长、系学生会办公室副部长',
                  school='清华大学', org='清华大学')
        c.elements = ['组织=校科协', '组织=系学生会', '职务=赛事部部长', '职务=办公室副部长']
        plan = build_plan(c, '林示例', ResumeProfile(identity='student'))
        wechat = [q for q in plan.queries if q.site == WECHAT_HOST]
        self.assertEqual({q.text for q in wechat}, {
            '清华大学 林示例 赛事部部长', '清华大学 林示例 办公室副部长',
            '清华大学 校科协 换届', '清华大学 系学生会 换届',
        })
        self.assertTrue(all(q.round == 1 for q in wechat))
        self.assertTrue(any(q.site != WECHAT_HOST for q in plan.queries))

    def test_student_wechat_records_index_hits_without_reading_disallowed_articles(self):
        from app.collect import Budget, collect_for_claim
        from app.data_sources import AgentRun, SourceFetcher, SourceSearcher
        from app.plan import WECHAT_HOST
        from app.source_guard import SourceGuardSearcher
        from app.verification_agents import agent_for

        c = claim('学生工作', '2025年林示例在清华大学学生会担任部长', school='清华大学')
        c.elements = ['组织=学生会', '职务=部长']
        plan = build_plan(c, '林示例', ResumeProfile(identity='student'))
        plan.queries = [next(q for q in plan.queries if q.source == 'wechat:student_work' and '林示例' in q.text)]
        article = 'https://mp.weixin.qq.com/s/abc_123'
        search = Mock()
        search.search.return_value = [SearchHit(article, title='学生会换届公示', snippet='林示例担任部长'),
                                      SearchHit('https://mp.weixin.qq.com/mp/profile_ext', title='公众号主页'),
                                      SearchHit('https://mp.weixin.qq.com.example/s/abc', title='无关链接'),
                                      SearchHit('https://mp.weixin.qq.com/s/excluded', publisher='北京大学学生会')]
        fetcher = Mock()
        budget = Budget(max_searches=1, max_page_reads=3, max_seconds=20)
        run = AgentRun(agent_for(c), c, plan, budget)
        guarded = SourceGuardSearcher(search, policy(domains=['pku.edu.cn'], organizations=['北京大学']))
        with run.bind(), patch('app.search.robots_allows', return_value=False):
            evidence, _ = collect_for_claim(c, '林示例', SourceSearcher(guarded, run), Mock(),
                                            budget=budget, fetcher=SourceFetcher(fetcher, run), queries=plan.queries)
        search.search.assert_called_once_with(plan.queries[0].text, site=WECHAT_HOST)
        fetcher.get_roster.assert_not_called()
        self.assertEqual(evidence, [])
        self.assertTrue(any(e['sourceId'] == 'student_wechat' and e['state'] == 'returned' for e in run.events))
        self.assertEqual([d['reference'] for d in run.decisions if d['state'] == 'navigation'], [article])

    def project(self,raw=None,employed=False):
        c=claim('项目',raw or '林示例清华大学研究生教育教学改革研究项目，项目编号：25Z-AA017',school='清华大学',title='研究生教育教学改革研究项目')
        if employed:c.entities['candidate_identity_context']['current_employers']=['清华大学']
        sources=policy(domains=['tsinghua.edu.cn'],organizations=['清华大学'])
        sources.update(domains_by_org={'清华大学':['tsinghua.edu.cn']},current_employers=['清华大学'] if employed else [])
        return c,sources

    def test_numbered_student_academic_grant_can_use_issuing_school(self):
        c,s=self.project();self.assertFalse(excluded('https://www.tsinghua.edu.cn/grants/1',for_claim(c,s)))

    def test_actual_employer_still_excluded_for_project(self):
        c,s=self.project(employed=True);self.assertTrue(excluded('https://www.tsinghua.edu.cn/grants/1',for_claim(c,s)))

    def test_generic_or_unnumbered_project_cannot_open_school_exception(self):
        for raw in ('林示例清华大学软件项目，项目编号：25Z-AA017','林示例清华大学研究生教育教学改革研究项目'):
            c,s=self.project(raw);self.assertTrue(excluded('https://www.tsinghua.edu.cn/grants/1',for_claim(c,s)))
