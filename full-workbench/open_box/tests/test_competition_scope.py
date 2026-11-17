"""明确年份、国省赛及省份必须与支持该奖项的来源对应。"""
import unittest

from app.collect import extract_evidence
from app.competitions import (claim_level, competition_level, competition_for_claim,
                              publication_note, seed_pages)
from app.schema import Claim
from app.search import SearchHit
from app.strategy import build_plan


def claim(stage='国赛', province=''):
    return Claim(id='scope',raw_text=f'2021 全国大学生数学建模竞赛 {province}{stage} 二等奖',
                 raw_locator='测试',category='竞赛',date_label='2021',
                 elements=['年份=2021',f'阶段={stage}','奖级=二等奖'],
                 entities={'contest':'全国大学生数学建模竞赛','school':'北京大学'})


def evidence(c, snippet, title='2021获奖名单',url='https://www.mcm.edu.cn/test.pdf'):
    class Model:
        def complete_json(self,*args,**kwargs):
            return dict(snippet=snippet,supports=c.elements,publisher='竞赛组委会',
                        identity_signals=['学校一致'],contradicts=[])
    return extract_evidence(c,SearchHit(url,title=title),snippet,Model(),candidate_name='张三')


class CompetitionScopeTests(unittest.TestCase):
    def test_original_provincial_stage_wins_over_wrong_model_level(self):
        c=claim('省赛');c.entities['level']='国家级'
        self.assertEqual(claim_level(c),'provincial')
        self.assertFalse(any(p.get('level')=='national' for p in seed_pages(competition_for_claim(c),c)))
        self.assertTrue(any('2021' in q.text and '省赛' in q.text for q in build_plan(c,'张三').queries))

    def test_national_round_spellings_are_recognized(self):
        for text in ('全国赛','全国决赛','国家二等奖','全国奖','国赛','国家级二等奖'):
            self.assertEqual(competition_level(text),'national',text)
        self.assertEqual(competition_level('全国大学生数学建模竞赛广东省赛二等奖'),'provincial')

    def test_matching_year_and_national_round_support_award(self):
        ev=evidence(claim(),'2021国赛 北京大学 张三 二等奖')
        self.assertIn('奖级=二等奖',ev.supports)
        self.assertIn('年份=2021',ev.supports)

    def test_provincial_and_national_awards_cannot_substitute_each_other(self):
        for requested,found in [('国赛','省赛'),('省赛','国赛')]:
            ev=evidence(claim(requested),f'2021{found} 北京大学 张三 二等奖')
            self.assertTrue(ev is None or '奖级=二等奖' not in ev.supports)

    def test_source_without_round_cannot_prove_explicit_national_award(self):
        ev=evidence(claim(),'2021 北京大学 张三 二等奖')
        self.assertIn('年份=2021',ev.supports)
        self.assertNotIn('奖级=二等奖',ev.supports)

    def test_source_without_year_cannot_prove_dated_award(self):
        ev=evidence(claim(),'国赛 北京大学 张三 二等奖',title='获奖名单')
        self.assertNotIn('奖级=二等奖',ev.supports)
        self.assertNotIn('年份=2021',ev.supports)

    def test_other_year_same_level_does_not_prove_award_or_accuse_candidate(self):
        ev=evidence(claim(),'2022国赛 北京大学 张三 二等奖',title='2022获奖名单')
        self.assertEqual(ev.supports,[])
        self.assertEqual(ev.contradicts,[])

    def test_explicit_province_is_required_to_match(self):
        c=claim('省赛','广东')
        for found in ('广东省赛','福建省赛','省赛'):
            ev=evidence(c,f'2021 {found} 北京大学 张三 二等奖')
            self.assertEqual('奖级=二等奖' in ev.supports,found=='广东省赛')

    def test_verified_national_pdf_header_supplies_scope(self):
        url='https://www.mcm.edu.cn/upload_cn/node/626/n7nhofCn201ab6d8d92db35564daa7cca31853d6.pdf'
        ev=evidence(claim(),'北京大学 张三 二等奖',title='2021高教社杯全国大学生数学建模竞赛获奖名单.pdf',url=url)
        self.assertIn('奖级=二等奖',ev.supports)
        self.assertIn('年份=2021',ev.supports)

    def test_year_and_scope_are_explicit_in_report_notes(self):
        c=claim('省赛','广东')
        note=publication_note(competition_for_claim(c),c)
        self.assertIn('2021',note)
        self.assertIn('广东省赛',note)

    def test_uncatalogued_competition_obeys_year_and_scope_checks(self):
        c=claim('省赛');c.entities['contest']='测试程序设计竞赛'
        c.raw_text='2021 测试程序设计竞赛 省赛二等奖'
        ev=evidence(c,'2022省赛 北京大学 张三 二等奖',title='2022省赛获奖名单')
        self.assertEqual(ev.supports,[])
        self.assertTrue(evidence(c,'2021国赛 北京大学 张三 二等奖') is None)

    def test_mixed_summary_uses_candidate_row_level(self):
        ev=evidence(claim('省赛','广东'),'2021广东省赛 北京大学 张三 二等奖',
                    title='2021国赛及省赛获奖汇总')
        self.assertIn('奖级=二等奖',ev.supports)

    def test_two_years_bind_each_stage_to_its_own_year(self):
        text='张三 2021年省赛二等奖；2022年国赛二等奖'
        ev=evidence(claim('国赛'),text,title='历年竞赛获奖汇总')
        self.assertNotIn('奖级=二等奖',ev.supports)
        ev=evidence(claim('省赛'),text,title='历年竞赛获奖汇总')
        self.assertIn('奖级=二等奖',ev.supports)

    def test_admission_or_graduation_year_cannot_supply_award_year(self):
        ev=evidence(claim(),'张三2021年入学，2020年国赛二等奖',title='人物介绍')
        self.assertEqual(ev.supports,[])
        ev=evidence(claim(),'张三国赛二等奖',title='2021优秀毕业生人物介绍')
        self.assertEqual(ev.supports,[])


if __name__=='__main__':unittest.main()
