"""赛事路由、年度附件、个人归属和名单不可用措辞的回归测试。"""
import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import judge
from app.collect import Budget, _focus_text, _relevance, collect_for_claim, extract_evidence
from app.competitions import (catalog_query_specs, competition_for_claim, find_competition,
                              load_catalog, lookup_summary, publication_note, roster_links, seed_pages)
from app.schema import Claim, Report
from app.search import PageFetcher, SearchHit, _award_table_text
from app.strategy import ResumeProfile, add_competition_school_hints, build_plan


def claim(contest="CISCN", year="2024", **entities):
    return Claim(id="award",raw_text=f"{year} {contest} 一等奖",raw_locator="第1页",
                 category="竞赛",date_label=year,
                 elements=[f"赛事={contest}","奖项=一等奖",f"年份={year}","参赛身份=成员"],
                 entities={"contest":contest,"school":"北京大学",**entities})


class SearchMustNotRun:
    def search(self,*a,**k):
        raise AssertionError("完整官网证据已找到，不应再调用付费搜索")


class FullModel:
    def __init__(self,item,snippet="2024 北京大学 张三 Demo队 一等奖"):
        self.item=item
        self.snippet=snippet
        self.calls=0

    def complete_json(self,*args,**kwargs):
        self.calls+=1
        return dict(snippet=self.snippet,publisher="竞赛组委会",
                    published_at="2024-08-01",identity_signals=["学校一致","队友或合作者一致"],
                    supports=self.item.elements,contradicts=[],identity_conflicts=[])


class CompetitionTests(unittest.TestCase):
    def test_catalog_has_complete_base_directory_and_international_extension(self):
        catalog=load_catalog()
        base=[r for r in catalog["competitions"] if r.get("directory_order")]
        self.assertEqual(len(base),84)
        self.assertGreaterEqual(len(catalog["competitions"]),100)
        self.assertGreaterEqual(len({s for r in base for s in r["subjects"]}),10)

    def test_aliases_do_not_mix_domestic_and_overseas_math_contests(self):
        for text in ["国赛数学建模","2024 高教社杯全国大学生数学建模竞赛","CUMCM"]:
            self.assertEqual(find_competition(text)["id"],"cumcm")
        for text in ["美赛","MCM/ICM 2024","美国大学生数学建模竞赛"]:
            self.assertEqual(find_competition(text)["id"],"mcm-icm")

    def test_latin_acronyms_have_word_boundaries_and_accept_fullwidth(self):
        self.assertIsNone(find_competition("IMMC XICPC ABCMCM"))
        self.assertEqual(find_competition("ＩＣＰＣ 2024 World Finals")["id"],"icpc")

    def test_ambiguous_challenge_cup_is_not_guessed(self):
        self.assertIsNone(find_competition("2024 挑战杯 金奖"))
        self.assertEqual(find_competition("挑战杯课外学术科技作品竞赛")["id"],"challenge-academic")
        self.assertEqual(find_competition("挑战杯创业计划竞赛")["id"],"challenge-business")

    def test_longer_worldskills_selection_name_wins(self):
        self.assertEqual(find_competition("世界技能大赛中国选拔赛")["id"],"worldskills-china")

    def test_missing_entity_can_match_original_resume(self):
        item=claim("全国大学生数学建模竞赛")
        item.entities={}
        item.elements=["奖项=一等奖"]
        self.assertEqual(competition_for_claim(item)["id"],"cumcm")
        self.assertTrue(any(q.site=="mcm.edu.cn" for q in build_plan(item,"张三").queries))

    def test_other_categories_do_not_gain_competition_routing(self):
        item=claim("iGEM")
        item.category="项目"
        self.assertIsNone(competition_for_claim(item))

    def test_official_roster_search_precedes_person_and_school_queries(self):
        plan=build_plan(claim("全国大学生数学建模竞赛"),"张三",ResumeProfile(identity="student"))
        self.assertEqual(plan.queries[0].site,"mcm.edu.cn")
        self.assertIn("获奖名单",plan.queries[0].text)
        self.assertNotIn("张三",plan.queries[0].text)
        self.assertTrue(any(q.site=="pku.edu.cn" for q in plan.queries))
        self.assertLessEqual(len(plan.queries),plan.budget["searches"])

    def test_contest_year_entity_and_team_number_are_used(self):
        item=claim("MCM",year="",team_id="2400123")
        item.entities["year"]="2024"
        specs=catalog_query_specs(find_competition("MCM"),item,"张三")
        self.assertTrue(any("2400123" in q["text"] and "2024" in q["text"] for q in specs))

    def test_moe_results_are_searched_as_an_official_alternate(self):
        plan=build_plan(claim("中国国际大学生创新大赛",year="2025"),"张三")
        self.assertTrue(any(q.site=="moe.gov.cn" for q in plan.queries))

    def test_english_school_name_routes_to_registered_official_domain(self):
        from app.plan import school_domain
        self.assertEqual(school_domain('Beijing Normal-Hong Kong Baptist University'),'bnbu.edu.cn')

    def test_current_education_adds_search_hint_without_inventing_contest_school(self):
        edu=claim(); edu.category='学历';edu.date_start='2023-09';edu.date_end='2027-06'
        edu.entities={'school':'Beijing Normal-Hong Kong Baptist University'}
        c=claim('MCM','2026');c.entities={'contest':'MCM','year':'2026'}
        original=list(c.elements);raw=c.raw_text
        add_competition_school_hints([edu,c])
        self.assertIn('school_search_hint',c.entities)
        self.assertNotIn('school',c.entities)
        self.assertEqual(c.elements,original);self.assertEqual(c.raw_text,raw)
        self.assertTrue(any(q.site=='bnbu.edu.cn' and '获奖名单' in q.text for q in build_plan(c,'Sun Haoran').queries))

    def test_overlapping_education_does_not_guess_contest_school(self):
        schools=[]
        for name in ['北京大学','清华大学']:
            edu=claim();edu.category='学历';edu.date_start='2023';edu.date_end='2027';edu.entities={'school':name}
            schools.append(edu)
        c=claim('MCM','2025');c.entities={'contest':'MCM'}
        add_competition_school_hints([*schools,c])
        self.assertNotIn('school_search_hint',c.entities)

    def test_member_role_is_not_used_as_a_team_name(self):
        self.assertFalse(any('"成员"' in q.text for q in build_plan(claim('MCM'),'张三').queries))

    def test_comap_does_not_search_a_paper_title_or_repeat_generic_queries(self):
        c=claim('MCM',project='An Extremely Long Project Title '+ 'A'*150)
        queries=build_plan(c,'Sun Haoran').queries
        official=[q for q in queries if q.site=='contest.comap.com']
        self.assertEqual(len(official),1)
        self.assertIn('MCM ICM',official[0].text)
        self.assertNotIn('Extremely',official[0].text)

    def test_fixed_year_rosters_are_not_reused_for_wrong_or_missing_year(self):
        competition=find_competition("中国国际大学生创新大赛")
        self.assertEqual(seed_pages(competition,claim(competition["name"],"2024")),[])
        self.assertEqual(seed_pages(competition,claim(competition["name"],"")),[])
        self.assertEqual(seed_pages(competition,claim(competition["name"],"2025"))[0]["year"],2025)

    def test_roster_following_rejects_wrong_year_and_external_urls(self):
        html='''<html><a href="/2023/result.pdf">2023获奖名单</a>
        <a href="https://other.example/2024.pdf">2024获奖名单</a>
        <a href="/2024/result.pdf">2024获奖名单</a></html>'''
        links=roster_links(html,SearchHit("https://www.ciscn.cn/announcement/view/357",title="历届获奖名单"),claim())
        self.assertEqual([x["url"] for x in links],["https://www.ciscn.cn/2024/result.pdf"])

    def test_comap_year_in_parent_row_routes_to_correct_results(self):
        html='''<table><tr><td>2023</td><td><a href="/2023/results/">Results</a></td></tr>
        <tr><td>2024</td><td><a href="/2024/results/">Results</a></td></tr></table>'''
        links=roster_links(html,SearchHit("https://contest.comap.com/archive",title="Previous contests"),claim("MCM"))
        self.assertEqual(len(links),1)
        self.assertIn("2024",links[0]["url"])

    def test_comap_year_result_precedes_generic_navigation_and_self_link(self):
        url="https://contest.comap.com/undergraduate/contests/mcm/previous-contests.php"
        html='''<a href="previous-contests.php">Problems and Results</a>
        <a href="../matrix/">Matrix of results</a><li>2025<ul><li><a href="contests/2025/results">Results</a></li></ul></li>
        <li>2024<ul><li><a href="contests/2024/results">Results</a></li></ul></li>'''
        links=roster_links(html,SearchHit(url,title="Previous contests"),claim("MCM"),limit=1)
        self.assertEqual(links[0]["url"],"https://contest.comap.com/undergraduate/contests/mcm/contests/2024/results")

    def test_matching_track_precedes_other_award_attachments(self):
        item=claim("全国大学生职业规划大赛",track="就业赛道")
        html='''<a href="/growth.docx">成长赛道获奖名单</a><a href="/job.docx">就业赛道获奖名单</a>'''
        links=roster_links(html,SearchHit("https://www.moe.gov.cn/notice.html",title="2024获奖名单"),item,limit=1)
        self.assertEqual(links[0]["url"],"https://www.moe.gov.cn/job.docx")

    def test_comap_complete_results_precede_scholarship_and_filter_contest_type(self):
        html='''<a href="/2024/scholarship.pdf">International Scholarship Award Winners</a>
        <a href="/2024/a.pdf">Download the complete MCM Problem A results report (pdf)</a>
        <a href="/2024/b.pdf">Download the complete MCM Problem B results report (pdf)</a>
        <a href="/2024/c.pdf">Download the complete MCM Problem C results report (pdf)</a>
        <a href="/2024/d.pdf">Download the complete ICM Problem D results report (pdf)</a>'''
        page=SearchHit("https://contest.comap.com/2024/results/",title="2024 Results")
        links=roster_links(html,page,claim("MCM",team_id="2400123"))
        self.assertEqual([x['url'] for x in links[:3]],['https://contest.comap.com/2024/'+v+'.pdf' for v in 'abc'])
        self.assertFalse(any(x['url'].endswith('/d.pdf') for x in links))

    def test_missing_comap_team_number_preserves_school_read_budget(self):
        html='<a href="/2026/a.pdf">Download the complete MCM Problem A results report</a>'
        c=claim('MCM','2026')
        self.assertEqual(roster_links(html,SearchHit('https://contest.comap.com/results/',title='2026 Results'),c),[])

    def test_provincial_award_does_not_seed_national_roster_and_searches_school_first(self):
        c=claim('CUMCM','2025',level='省赛')
        c.raw_text='2025 CUMCM Provincial Second Prize'
        self.assertEqual(seed_pages(competition_for_claim(c),c),[])
        self.assertEqual(build_plan(c,'张三').queries[0].site,'pku.edu.cn')

    def test_school_current_year_roster_precedes_same_name_unrelated_webpage(self):
        c=claim('MCM','2026');c.entities['school']='BNBU'
        roster=SearchHit('https://bnbu.edu.cn/list.pdf',title='2026美国大学生数学建模竞赛获奖名单')
        unrelated=SearchHit('https://dblp.org/author/sun',title='Sun Haoran 2024 publications')
        self.assertGreater(_relevance(roster,'Sun Haoran',c),_relevance(unrelated,'Sun Haoran',c))

    def test_fixed_year_result_does_not_also_read_archive_index(self):
        pages=seed_pages(find_competition('MCM'),claim('MCM','2025'))
        self.assertEqual(len(pages),1)
        self.assertEqual(pages[0]['year'],2025)

    def test_malformed_comap_list_does_not_score_navigation_as_yearly_results(self):
        html='''<li>2025<ul><li><a href="/2025/results">Results</a></li></ul>
        <li>2024<ul><li><a href="/2024/results">Results</a></li></ul>
        <a href="/matrix">Matrix of all problems and results</a>'''
        links=roster_links(html,SearchHit("https://contest.comap.com/archive",title="Previous contests"),claim("MCM"))
        self.assertEqual([x['url'] for x in links],['https://contest.comap.com/2024/results'])

    def test_ncda_teacher_competition_is_not_followed(self):
        html='<a href="/teacher-awards.pdf">2024教创赛教师获奖名单</a><a href="/student-awards.pdf">2024学生获奖名单</a>'
        links=roster_links(html,SearchHit("https://www.ncda.org.cn/dsjs/hjmd/",title="获奖名单"),claim("NCDA"))
        self.assertEqual(len(links),1)
        self.assertIn("student",links[0]["url"])

    def test_teacher_award_is_not_followed_as_student_award(self):
        html='<a href="/teacher.pdf">2024优秀指导教师获奖名单</a><a href="/student.pdf">2024学生与指导教师获奖名单</a>'
        links=roster_links(html,SearchHit("https://www.ciscn.cn/announcement/",title="获奖名单"),claim())
        self.assertEqual([x["url"] for x in links],["https://www.ciscn.cn/student.pdf"])

    def test_school_script_embedded_pdf_is_followed_without_executing_script(self):
        c=claim('MCM','2026');c.entities['school']='Beijing Normal-Hong Kong Baptist University'
        html='''<p>2026年BNBU美赛获奖名单</p><p><script>
        showVsbpdfIframe("/virtual_attach_file.vsb?afc=public&oid=12&e=.pdf","100%","600")
        </script></p>'''
        page=SearchHit('https://bnbu.edu.cn/info/news.htm',title='数模竞赛，历史新高')
        links=roster_links(html,page,c)
        self.assertEqual(len(links),1)
        self.assertIn('virtual_attach_file.vsb',links[0]['url'])
        self.assertIn('2026',links[0]['title'])

    def test_unrelated_school_cannot_expand_attachment_scope(self):
        c=claim('MCM','2026');c.entities['school']='北京大学'
        html='<p>2026年获奖名单</p><script>showVsbpdfIframe("/results.pdf")</script>'
        self.assertEqual(roster_links(html,SearchHit('https://unrelated.example/news',title='获奖名单'),c),[])

    def test_pdf_award_column_continues_across_page_and_normalizes_cjk_forms(self):
        class Table:
            def __init__(self,rows):self.rows=rows
            def extract(self):return self.rows
        class Page:
            def __init__(self,rows):self.rows=rows
            def get_text(self):return '2025获奖名单 奖项 姓名 专业'
            def find_tables(self):
                from types import SimpleNamespace
                return SimpleNamespace(tables=[Table(self.rows)])
        pages=[Page([['奖项','姓名','专业'],['⼴东省⼆等奖','李四','统计学']]),
               Page([['','张三','商业分析'],['广东省三等奖','王五','统计学']])]
        text=_award_table_text(pages)
        self.assertIn('广东省二等奖 | 张三 | 商业分析',text)
        self.assertNotIn('广东省三等奖 | 张三',text)

    def test_pdf_unrelated_table_does_not_inherit_an_award(self):
        class Table:
            def __init__(self,rows):self.rows=rows
            def extract(self):return self.rows
        class Page:
            def __init__(self,rows):self.rows=rows
            def get_text(self):return '奖项 姓名'
            def find_tables(self):
                from types import SimpleNamespace
                return SimpleNamespace(tables=[Table(self.rows)])
        pages=[Page([['奖项','姓名','专业'],['一等奖','李四','统计学']]),
               Page([['奖级','姓名','学校','专业'],['','张三','某校','商业分析']])]
        self.assertNotIn('张三',_award_table_text(pages))

    def test_team_number_at_end_of_long_roster_is_retained(self):
        item=claim("MCM",team_id="2400123")
        text="2024 Results\n" + "其他队伍\n"*9000 + "2400123 北京大学 Meritorious Winner"
        self.assertIn("2400123 北京大学",_focus_text(text,item,"张三"))

    def test_direct_notice_and_attachment_confirm_without_search_fee(self):
        item=claim()
        notice="https://www.ciscn.cn/announcement/view/357"
        urls=[]
        class Pages:
            failures={}
            def get(self,url):
                urls.append(url)
                return '<a href="/2024/winners.pdf">2024获奖名单</a>' if url==notice else "2024 北京大学 张三 Demo队 一等奖"
        budget=Budget(max_searches=2,max_page_reads=3)
        model=FullModel(item)
        evidence,exhausted=collect_for_claim(item,"张三",SearchMustNotRun(),model,
                                            budget=budget,fetcher=Pages(),queries=build_plan(item,"张三").queries)
        self.assertEqual(judge.assess(item,evidence).status,"ok")
        self.assertEqual(budget.searches,0)
        self.assertEqual(budget.page_reads,2)
        self.assertEqual(model.calls,1)
        self.assertFalse(exhausted)
        self.assertIn("https://www.ciscn.cn/2024/winners.pdf",urls)

    def test_attachment_reading_cannot_exceed_budget(self):
        item=claim()
        class Pages:
            def get(self,url):
                return '<a href="/2024/winners.pdf">2024获奖名单</a>'
        budget=Budget(max_searches=0,max_page_reads=1)
        evidence,_=collect_for_claim(item,"张三",SearchMustNotRun(),FullModel(item),
                                    budget=budget,fetcher=Pages(),queries=[])
        self.assertEqual(budget.page_reads,1)
        self.assertEqual(evidence,[])

    def test_generic_contest_homepage_does_not_consume_model_call(self):
        class Pages:
            def get(self,url):
                return '<html><title>竞赛官网</title><p>竞赛简介及报名通知</p></html>'
        item=claim()
        model=FullModel(item)
        evidence,_=collect_for_claim(item,"张三",SearchMustNotRun(),model,
                                    budget=Budget(max_searches=0,max_page_reads=2),fetcher=Pages(),queries=[])
        self.assertEqual(model.calls,0)
        self.assertEqual(evidence,[])

    def test_redirected_notice_uses_final_directory_for_relative_attachment(self):
        item=claim()
        notice="https://www.ciscn.cn/announcement/view/357"
        read=[]
        class Pages:
            resolved_urls={notice:notice+'/'}
            def get(self,url):
                read.append(url)
                return '<a href="winners.pdf">2024获奖名单</a>' if url==notice else '2024 北京大学 张三 Demo队 一等奖'
        evidence,_=collect_for_claim(item,"张三",SearchMustNotRun(),FullModel(item),
                                    budget=Budget(max_searches=0,max_page_reads=3),fetcher=Pages(),queries=[])
        self.assertIn(notice+'/winners.pdf',read)
        self.assertEqual(judge.assess(item,evidence).status,'ok')

    def test_page_fetcher_records_redirect_destination(self):
        import httpx
        original='https://contest.comap.com/2024/results'
        response=httpx.Response(200,text='<p>Results</p>',request=httpx.Request('GET',original+'/'))
        fetcher=PageFetcher()
        with patch('app.search.robots_allows',return_value=True),patch('httpx.get',return_value=response):
            self.assertTrue(fetcher.get(original))
        self.assertEqual(fetcher.resolved_urls[original],original+'/')

    def test_english_name_case_does_not_skip_official_roster(self):
        text="2024 北京大学 JOHN DOE Demo队 一等奖"
        class Pages:
            def get(self,url):
                return f'<html><title>2024获奖名单</title><p>{text}</p></html>'
        item=claim()
        model=FullModel(item,text)
        evidence,_=collect_for_claim(item,"John Doe",SearchMustNotRun(),model,
                                    budget=Budget(max_searches=0,max_page_reads=2),fetcher=Pages(),queries=[])
        self.assertEqual(model.calls,1)
        self.assertEqual(judge.assess(item,evidence).status,"ok")

    def test_team_result_without_person_does_not_confirm_membership(self):
        item=claim("MCM")
        model=FullModel(item,"2024 北京大学 队号2400123 Meritorious Winner")
        ev=extract_evidence(item,SearchHit("https://contest.comap.com/results",title="2024 Results"),
                            "2024 北京大学 队号2400123 Meritorious Winner",model,
                            organizer_hosts={"comap.com"},candidate_name="张三",candidate_provided=True)
        self.assertNotEqual(judge.assess(item,[ev]).status,"ok")
        self.assertNotIn("参赛身份=成员",ev.supports)
        self.assertNotIn("学校一致",ev.identity_signals)
        self.assertNotIn("候选人自提供该链接",ev.identity_signals)

    def test_unquoted_model_claim_is_not_award_evidence(self):
        item=claim()
        ev=extract_evidence(item,SearchHit("https://www.ciscn.cn/results",title="2024获奖名单"),
                            "2024 本页只有另一支队伍",FullModel(item),
                            organizer_hosts={"ciscn.cn"},candidate_name="张三")
        self.assertIsNone(ev)

    def test_candidate_name_is_given_to_model_and_other_person_row_cannot_prove_claim(self):
        item=claim()
        model=FullModel(item,'2024 北京大学 李四 Demo队 一等奖')
        model.prompt=''
        original=model.complete_json
        def capture(system,*args,**kwargs):
            model.prompt=system
            return original(system,*args,**kwargs)
        model.complete_json=capture
        ev=extract_evidence(item,SearchHit('https://www.ciscn.cn/results',title='2024获奖名单'),
                            '2024 北京大学 张三 另一队 二等奖\n2024 北京大学 李四 Demo队 一等奖',model,
                            organizer_hosts={'ciscn.cn'},candidate_name='张三')
        self.assertIn('当前候选人姓名',model.prompt)
        self.assertIn('张三',model.prompt)
        self.assertNotIn('学校一致',ev.identity_signals)
        self.assertNotEqual(judge.assess(item,[ev]).status,'ok')

    def test_other_year_award_cannot_support_current_year_or_accuse_candidate(self):
        item=claim()
        ev=extract_evidence(item,SearchHit("https://www.ciscn.cn/results",title="2025获奖名单"),
                            "2025 北京大学 张三 Demo队 一等奖",FullModel(item,"2025 北京大学 张三 Demo队 一等奖"),
                            organizer_hosts={"ciscn.cn"},candidate_name="张三")
        self.assertEqual(ev.supports,[])
        self.assertEqual(ev.contradicts,[])
        self.assertNotEqual(judge.assess(item,[ev]).status,"ok")

    def test_provisional_roster_cannot_prove_final_award(self):
        item=claim()
        ev=extract_evidence(item,SearchHit("https://www.ciscn.cn/results",title="2024获奖名单（初稿）"),
                            "2024 北京大学 张三 Demo队 一等奖",FullModel(item),
                            organizer_hosts={"ciscn.cn"},candidate_name="张三")
        self.assertNotIn("奖项=一等奖",ev.supports)
        self.assertNotEqual(judge.assess(item,[ev]).status,"ok")

    def test_provisional_header_in_attachment_is_also_checked(self):
        item=claim()
        ev=extract_evidence(item,SearchHit("https://www.ciscn.cn/results",title="获奖名单附件"),
                            "2024拟获奖名单\n2024 北京大学 张三 Demo队 一等奖",FullModel(item),
                            organizer_hosts={"ciscn.cn"},candidate_name="张三")
        self.assertNotIn("奖项=一等奖",ev.supports)

    def test_birth_year_does_not_override_explicit_contest_year(self):
        item=claim()
        text="张三 2003年出生 北京大学 Demo队 一等奖"
        ev=extract_evidence(item,SearchHit("https://www.ciscn.cn/results",title="2024获奖名单"),
                            text,FullModel(item,text),organizer_hosts={"ciscn.cn"},candidate_name="张三")
        self.assertIn("奖项=一等奖",ev.supports)

    def test_undated_fixed_roster_and_login_portal_are_not_seeded(self):
        competition=copy.deepcopy(find_competition("CISCN"))
        competition['result_pages']=[dict(url='https://www.ciscn.cn/results',kind='roster',year=None),
                                     dict(url='https://www.ciscn.cn/login',kind='index',requires_login=True)]
        self.assertEqual(seed_pages(competition,claim()),[])

    def test_english_results_are_official_only_on_verified_domains(self):
        self.assertEqual(judge.classify_tier("https://contest.comap.com/results","2024 Winners and Results",
                                            organizer_hosts={"comap.com"}),"A")
        self.assertEqual(judge.classify_tier("https://unrelated.example/results","2024 Winners and Results",
                                            organizer_hosts={"comap.com"}),"D")

    def test_failed_search_or_page_never_means_no_public_roster(self):
        item=copy.deepcopy(find_competition("蓝桥杯"))
        item["publication"]["status"]="unknown"
        note=publication_note(item)
        self.assertIn("不代表官网不提供",note)
        self.assertNotIn("官方说明不公开",note)

    def test_not_public_status_requires_documented_policy(self):
        doc=copy.deepcopy(load_catalog())
        doc["competitions"][0]["publication"]=dict(status="not_public",source_url=None)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"catalog.json"
            path.write_text(__import__('json').dumps(doc))
            with self.assertRaises(ValueError):
                load_catalog(path)

    def test_not_public_policy_must_be_on_official_domain(self):
        doc=copy.deepcopy(load_catalog())
        doc["competitions"][0]["publication"]=dict(status="not_public",source_url="https://fake.example/policy",
                                                   policy_quote="不公开名单")
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"catalog.json"
            path.write_text(__import__('json').dumps(doc))
            with self.assertRaises(ValueError):
                load_catalog(path)

    def test_explicit_not_public_note_carries_official_policy_source(self):
        item=copy.deepcopy(find_competition("CISCN"))
        item['publication']=dict(status='not_public',source_url='https://www.ciscn.cn/policy',
                                 policy_quote='本赛项不公开获奖名单')
        self.assertIn(item['publication']['source_url'],publication_note(item))
        self.assertIn(item['publication']['policy_quote'],publication_note(item))

    def test_roster_scope_is_preserved_in_presentation_and_exports(self):
        source=Path(__file__).resolve().parents[2]/"underbox/serve.py"
        spec=importlib.util.spec_from_file_location("award_report_ui",source)
        ui=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ui)
        item=claim("MCM")
        verified=judge.assess(item,[])
        competition=competition_for_claim(item)
        verified.source_notes=[publication_note(competition)]
        verified.competition_lookup=lookup_summary(competition)
        rep=Report(id="award-test",candidate_name="张三",position="实习生",report_no="TEST",
                   generated_at="2026-10-07T00:00:00Z",claims=[verified])
        presentation=ui.to_presentation(rep)
        self.assertEqual(presentation["claims"][0]["sourceNotes"],verified.source_notes)
        self.assertEqual(presentation["claims"][0]["competitionLookup"]["id"],"mcm-icm")
        self.assertIn(verified.source_notes[0],ui.render_material(rep))
        markdown=ui.report_to_markdown(presentation)
        self.assertIn(verified.source_notes[0],markdown)
        self.assertIn(competition["official_urls"][0],markdown)


if __name__=="__main__":
    unittest.main()
