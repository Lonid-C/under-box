"""比赛年度与网页更新时间分开，报告和名单入口仅使用本条年度。"""
import unittest
import io
from unittest.mock import patch

from app.competitions import (claim_year, competition_for_claim, lookup_summary,
                              publication_note, roster_links, seed_pages)
from app.schema import Claim
from app.search import SearchHit
from app.strategy import build_plan


def award(year='2021'):
    return Claim(id='national', raw_text=f'{year}.09 参加第二十一届“高教社杯”全国大学生数学建模竞赛，获得国家级二等奖',
                 raw_locator='测试',category='竞赛',date_label=year,
                 elements=['赛事=全国大学生数学建模竞赛',f'年份={year}','奖级=国家级二等奖'],
                 entities={'contest':'全国大学生数学建模竞赛','year':year})


class CompetitionYearTests(unittest.TestCase):
    def test_original_year_overrides_incorrect_model_date_and_entity(self):
        c=award();c.entities['year']='2025';c.date_start='2025-09';c.date_label='2025.09'
        self.assertEqual(claim_year(c),2021)
        plan=build_plan(c,'张三')
        self.assertTrue(any('2021' in q.text for q in plan.queries))
        self.assertFalse(any('2025' in q.text for q in plan.queries))

    def test_original_year_is_used_when_model_omits_dates(self):
        c=award();c.entities.pop('year');c.date_label='';c.elements=[c.elements[0]]
        self.assertEqual(claim_year(c),2021)
        self.assertTrue(any('2021' in q.text for q in build_plan(c,'张三').queries))

    def test_2021_report_does_not_include_2025_pdf_status_or_urls(self):
        c=award();item=competition_for_claim(c)
        summary=lookup_summary(item,c)
        self.assertEqual(summary['target_year'],2021)
        self.assertNotIn('2025',publication_note(item,c))
        self.assertNotIn('扫描件',publication_note(item,c))
        self.assertIn('38页文本PDF',summary['scope_note'])
        self.assertTrue(any('5d988b4e' in url for url in summary['result_urls']))
        self.assertFalse(any('f1241bf' in url or 'QB2FQ76' in url for url in summary['result_urls']))

    def test_2025_report_keeps_only_its_pdf_status(self):
        c=award('2025');item=competition_for_claim(c)
        self.assertIn('扫描件',publication_note(item,c))
        self.assertNotIn('38页文本PDF',publication_note(item,c))
        self.assertFalse(any('5d988b4e' in url for url in lookup_summary(item,c)['result_urls']))

    def test_historical_direct_seed_is_for_2021_not_current_year(self):
        c=award();pages=seed_pages(competition_for_claim(c),c)
        self.assertEqual([p['year'] for p in pages],[2021])

    def test_2023_web_update_does_not_reject_2021_roster(self):
        c=award();item=competition_for_claim(c)
        html='''<h1>2021高教社杯全国大学生数学建模竞赛获奖名单</h1>
        <p>作者：www.mcm.edu.cn 2023-05-04</p>
        <a href='/upload_cn/node/626/list.pdf'>2021高教社杯全国大学生数学建模竞赛获奖名单.pdf</a>
        <a href='/2025.pdf'>2025高教社杯全国大学生数学建模竞赛获奖名单.pdf</a>'''
        links=roster_links(html,SearchHit(item['result_pages'][0]['url'],title='2021获奖名单'),c)
        self.assertEqual([p['url'] for p in links],['https://www.mcm.edu.cn/upload_cn/node/626/list.pdf'])

    def test_pipeline_uses_claim_specific_metadata(self):
        from app.pipeline import PreparedResume,verify_prepared
        from app.parse import ParsedDocument
        from app.strategy import ResumeProfile
        c=award()
        class Model:
            def complete_json(self,*args,**kwargs):return {'question':'请补充对应年份的获奖证明'}
        class Search:
            def search(self,*args,**kwargs):return []
        prepared=PreparedResume('test.txt',ParsedDocument(text='safe',risks=[],source='test.txt',kind='txt'),
                                Model(),ResumeProfile(identity='student'),[c],'张三')
        with patch('app.pipeline.collect_for_claim',return_value=([],False)):
            rep=verify_prepared(prepared,searcher=Search(),claim_ids=[c.id])
        vc=rep.claims[0]
        self.assertEqual(vc.competition_lookup['target_year'],2021)
        self.assertNotIn('2025',' '.join(vc.source_notes))

    def test_text_pdf_remains_readable_without_optional_parsers(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
        from app.search import _pdf_text
        writer=PdfWriter()
        page=writer.add_blank_page(width=200,height=200)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),
                               NameObject('/Subtype'):NameObject('/Type1'),
                               NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({
            NameObject('/F1'):writer._add_object(font)})})
        stream=DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 10 100 Td (2021 national roster) Tj ET')
        page[NameObject('/Contents')]=writer._add_object(stream)
        data=io.BytesIO();writer.write(data)
        with patch.dict('sys.modules',{'pymupdf':None,'pdfminer':None,'pdfminer.high_level':None}):
            text=_pdf_text(data.getvalue())
        self.assertIn('2021 national roster',text)

    def test_official_member_row_does_not_prove_captain_ratio_or_edition(self):
        from app.collect import extract_evidence
        c=award();c.elements+=['角色=队长','比例=0.1%','届次=第二十一届']
        snippet='2021 全国二等奖 北京大学 张三 李四 王五'
        class Model:
            def complete_json(self,*args,**kwargs):
                return dict(snippet=snippet,supports=c.elements,identity_signals=['学校一致'],
                            publisher='全国大学生数学建模竞赛组织委员会',
                            contradicts=['角色=队长：列为队员二，无队长标记',
                                         '比例=0.1%：未提供比例或统计口径'])
        ev=extract_evidence(c,SearchHit('https://www.mcm.edu.cn/roster.pdf',title='2021获奖名单'),
                            snippet,Model(),candidate_name='张三')
        self.assertIn('年份=2021',ev.supports)
        self.assertNotIn('角色=队长',ev.supports)
        self.assertNotIn('比例=0.1%',ev.supports)
        self.assertNotIn('届次=第二十一届',ev.supports)
        self.assertEqual(ev.contradicts,[])

    def test_long_roster_keeps_the_award_section_for_candidate_row(self):
        from app.collect import _focus_text
        text='2021 全国大学生数学建模竞赛\n本科组一等奖(292名)\n'+'第一奖项其他人\n'*3000
        text+='本科组二等奖(1071名)\n'+'第二奖项其他人\n'*3000
        text+='哈尔滨工程大学 张三 李四 王五\n'+'第二奖项其他人\n'*3000
        focused=_focus_text(text,award(),'张三')
        self.assertIn('本科组二等奖(1071名)',focused)
        self.assertIn('哈尔滨工程大学 张三 李四 王五',focused)
        self.assertLess(len(focused),13000)


if __name__=='__main__':unittest.main()
