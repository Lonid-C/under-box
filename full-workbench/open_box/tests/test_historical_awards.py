"""动态公告、历史届次、名单包及OCR的漏检回归。"""
import io
import json
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from app import judge
from app.collect import Budget, _read_and_extract, collect_for_claim
from app.competitions import competition_for_claim, roster_image_links, roster_links, seed_pages
from app.ocr import _lines
from app.roster_archive import archive_text
from app.schema import Claim, Evidence
from app.search import PageFetcher, SearchHit
from app.strategy import Query, ResumeProfile, build_plan


def claim(contest="蓝桥杯", year="2021", **entities):
    return Claim(id="history",raw_text=f"{year}{contest}算法设计三等奖",raw_locator="测试",
                 category="竞赛",date_label=year,elements=[f"赛事={contest}","奖级=三等奖"],
                 entities={"contest":contest,"school_search_hint":"哈尔滨工程大学",**entities})


def zipped(files):
    out=io.BytesIO()
    with zipfile.ZipFile(out,"w") as archive:
        for name,data in files.items():archive.writestr(name,data)
    return out.getvalue()


class HistoricalAwardTests(unittest.TestCase):
    def test_blue_short_name_school_and_edition_queries_are_prioritized(self):
        plan=build_plan(claim(),"林昱和",ResumeProfile(identity="student"))
        self.assertEqual(plan.queries[0].text,"蓝桥杯 2021 获奖名单")
        self.assertEqual(plan.queries[0].site,"hrbeu.edu.cn")
        self.assertTrue(any("第十二届" in q.text for q in plan.queries))
        self.assertTrue(any('"林昱和" 蓝桥杯'==q.text for q in plan.queries))
        self.assertLessEqual(sum(q.confirm_empty for q in plan.queries),2)

    def test_current_english_portals_are_not_seeds_for_2021(self):
        c=claim("全国高等院校英语能力大赛")
        self.assertEqual(seed_pages(competition_for_claim(c),c),[])
        self.assertEqual(build_plan(c,"林昱和").queries[0].site,"hrbeu.edu.cn")

    def test_school_specific_seed_is_not_used_for_another_school(self):
        c=claim()
        self.assertTrue(any("qihang" in p['url'] for p in seed_pages(competition_for_claim(c),c,limit=4)))
        c.entities['school_search_hint']='北京大学'
        self.assertFalse(any("qihang" in p['url'] for p in seed_pages(competition_for_claim(c),c)))

    def test_historical_archive_selects_edition_and_rar(self):
        h="""<table><tr><td>第十一届</td><td><a href='/old.rar'>第十一届决赛获奖名单.rar</a></td></tr>
        <tr><td>第十二届</td><td><a href='/202110/new.rar'>第十二届决赛获奖名单.rar</a></td></tr></table>"""
        links=roster_links(h,SearchHit('https://dasai.lanqiao.cn/notices/860/',title='历届获奖名单'),claim())
        self.assertEqual([p['url'] for p in links],['https://dasai.lanqiao.cn/202110/new.rar'])

    def test_dynamic_notice_reads_only_observed_public_get_endpoint(self):
        def response(data,url):return SimpleNamespace(content=data,status_code=200,url=url,headers={},raise_for_status=lambda:None)
        url='https://dasai.lanqiao.cn/notices/860/'
        api='https://www.guoxinlanqiao.com/api/web/news/selectone?nnid=860'
        shell=b'<html><title>Blue cup</title><div id="app"></div></html>'
        body=json.dumps({'news':{'title':'历届获奖名单','content':'<a href="https://upload.lanqiao.cn/a.rar">第十二届获奖名单</a>'}}).encode()
        with patch('app.search.robots_allows',return_value=True),patch('httpx.get',side_effect=[response(shell,url),response(body,api)]) as get:
            html=PageFetcher().get(url)
        self.assertIn('第十二届',html)
        self.assertEqual([call.args[0] for call in get.call_args_list],[url,api])

    def test_dynamic_empty_payload_is_failure_not_readable_title(self):
        response=SimpleNamespace(content=b'<html>app</html>',status_code=200,url='',headers={},raise_for_status=lambda:None)
        with patch('app.search.robots_allows',return_value=True),patch('httpx.get',return_value=response):
            f=PageFetcher();self.assertIsNone(f.get('https://dasai.lanqiao.cn/notices/860/'))
        self.assertIn('动态公告',f.failures['https://dasai.lanqiao.cn/notices/860/'])

    def test_nested_zip_reads_relevant_roster_without_writing_member_paths(self):
        nested=zipped({'软件类-黑龙江.pdf':b'%PDF-Heilongjiang','软件类-江苏.pdf':b'%PDF-Jiangsu','../escape.pdf':b'%PDF-escape'})
        data=zipped({'个人赛名单.zip':nested,'设计赛名单.pdf':b'%PDF-design'})
        result=archive_text(data,hint='算法 黑龙江',pdf_parser=lambda blob:blob.decode(),office_parser=lambda _:None)
        self.assertIn('Heilongjiang',result)
        self.assertNotIn('Jiangsu',result);self.assertNotIn('escape',result);self.assertNotIn('design',result)

    def test_recursion_is_bounded(self):
        data=zipped({'too-deep.zip':zipped({'next.zip':zipped({'next.zip':zipped({'end.pdf':b'%PDF-end'})})})})
        self.assertIsNone(archive_text(data,pdf_parser=lambda _: 'end',office_parser=lambda _:None))

    def test_ocr_keeps_row_columns_and_rejects_uncertain_names(self):
        rows=[{'text':'张三','confidence':.95,'top':.1,'left':.1,'height':.03},
              {'text':'北京大学','confidence':.98,'top':.101,'left':.4,'height':.03},
              {'text':'李四','confidence':.5,'top':.2,'left':.1,'height':.03}]
        self.assertEqual(_lines(rows),'张三 | 北京大学')

    def test_image_links_are_official_and_match_non_english_track(self):
        c=claim('全国高等院校英语能力大赛','2024');c.raw_text='2024非英语专业组省赛一等奖'
        html="""<article><p>【非英语专业组】</p><p><img src='/ueditor/php/upload/image/one.png'></p>
        <p>【英语专业组】</p><p><img src='/ueditor/php/upload/image/two.png'></p>
        <p><img src='https://evil.example/upload/roster.png'></p><img src='/upload/logo.png'></article>"""
        page=SearchHit('http://eaedu.org.cn/news-106.html',title='2024省赛获奖名单')
        self.assertEqual([p['url'] for p in roster_image_links(html,page,c)],['http://eaedu.org.cn/ueditor/php/upload/image/one.png'])

    def test_ocr_only_cannot_be_final_verified(self):
        c=claim();ev=Evidence(url='https://school.edu.cn/list.png',title='名单',publisher='学校',source_tier='A',
                            snippet='张三 一等奖',identity_score=4,identity_signals=['学校一致'],supports=c.elements,extraction_method='ocr')
        self.assertEqual(judge.decide(c,[ev]),'part');self.assertTrue(judge.needs_human('part',[ev]))
        ev.extraction_method='text';self.assertEqual(judge.decide(c,[ev]),'ok')

    def test_wrong_year_result_does_not_consume_reads_but_biography_can(self):
        c=claim('全国高等院校英语能力大赛')
        class Search:
            def search(self,*a,**k):return [SearchHit('https://hrbeu.edu.cn/2024',title='2024英语能力大赛获奖名单'),
                                          SearchHit('https://hrbeu.edu.cn/person',title='2023优秀毕业生林昱和人物介绍')]
        class Pages:
            calls=[]
            def get(self,url):self.calls.append(url);return '无相关个人奖项'
        class Model:
            def complete_json(self,*a,**k):return {}
        pages=Pages()
        collect_for_claim(c,'林昱和',Search(),Model(),fetcher=pages,queries=[Query('person')],budget=Budget(max_searches=1,max_page_reads=2))
        self.assertEqual(pages.calls,['https://hrbeu.edu.cn/person'])


if __name__=='__main__':unittest.main()
