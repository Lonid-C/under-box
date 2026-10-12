"""同校连续就读、推荐/录取与获奖独立取证回归。姓名均为合成数据。"""
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import judge, pipeline
from app.academic import (study_stage, with_academic_scope_context, award_scope_matches,
                          with_verified_education_context)
from app.collect import extract_evidence
from app.schema import Claim, Evidence, Report
from app.search import SearchHit
from app.strategy import ResumeProfile, add_admission_source_hints, build_plan

SCHOOL = "华南理工大学"
URL = "https://cnsba.scut.edu.cn/person.htm"


def degree(stage, start="", end=""):
    label = {"本科": "学士", "硕士": "硕士", "博士": "博士"}[stage]
    return Claim(id=stage, raw_text=f"{SCHOOL} {stage} 计算机科学 {start}—{end}",
                 raw_locator="测试", category="学历", date_label=f"{start}—{end}",
                 date_start=start or None, date_end=end or None,
                 elements=[f"学校={SCHOOL}", "专业=计算机科学", f"学位={label}"],
                 entities={"org": SCHOOL})


def award(stage="本科", year="2020"):
    return Claim(id="award", raw_text=f"{year} {stage}期间获得{SCHOOL}国家奖学金", raw_locator="测试",
                 category="奖学金", date_label=year,
                 elements=[f"授予单位={SCHOOL}", "奖项=国家奖学金"],
                 entities={"org": SCHOOL, "award": "国家奖学金"})


def evidence(text, supports, title="本人介绍"):
    return Evidence(url=URL, title=title, publisher=SCHOOL, source_tier="B", snippet=text,
                    identity_signals=["学校一致", "专业一致"], supports=supports)


def extract(claim, text, title="本人介绍"):
    model = SimpleNamespace(complete_json=lambda *a, **k: {
        "snippet": text, "publisher": SCHOOL, "identity_signals": ["学校一致", "专业一致"],
        "supports": claim.elements})
    return extract_evidence(claim, SearchHit(URL, title), text, model, candidate_name="张三")


class AcademicStageTests(unittest.TestCase):
    def test_same_school_master_and_doctor_do_not_prove_bachelor(self):
        for source in ("张三是华南理工大学2021级硕士研究生。", "张三是华南理工大学2022级博士生。"):
            self.assertIsNone(extract(degree("本科"), source))
        self.assertIsNone(extract(degree("硕士"), "张三获得华南理工大学计算机科学博士学位。"))
        self.assertIsNone(extract(degree("博士"), "张三获得华南理工大学计算机科学硕士学位。"))

    def test_source_qualification_is_bachelor_not_receiver_enrollment(self):
        text = "张三 计算机科学专业 华南理工大学"
        title = "本科生推荐免试攻读2021年硕士学位研究生资格名单"
        ev = extract(degree("本科"), text, title)
        self.assertIn("学校=华南理工大学", ev.supports)
        self.assertNotIn("学位=学士", ev.supports)
        self.assertIsNone(extract(degree("硕士"), text, title))
        self.assertIsNone(extract(degree("博士"), text, "本科推荐免试攻读博士研究生资格名单"))

    def test_actual_bachelor_and_master_keep_their_own_evidence(self):
        for stage in ("本科", "硕士", "博士"):
            c = degree(stage)
            text = f"张三获得{SCHOOL}计算机科学{c.elements[-1].partition('=')[2]}学位。"
            self.assertIn(c.elements[-1], extract(c, text).supports)

    def test_generic_graduate_record_does_not_choose_master_or_doctor(self):
        text = "张三在华南理工大学攻读研究生。"
        self.assertIsNone(extract(degree("硕士"), text))
        self.assertIsNone(extract(degree("博士"), text))

    def test_same_school_can_be_push_source(self):
        bs, ms = degree("本科", "2017-09", "2021-06"), degree("硕士", "2021-09", "2024-06")
        ms.raw_text += " 推荐免试"
        filled = add_admission_source_hints([bs, ms])[1]
        self.assertEqual(filled.entities["source_school_hint"], SCHOOL)
        self.assertEqual(filled.entities["source_study_stage_hint"], "本科")

    def test_verified_bachelor_guides_graduate_route_without_inventing_degree(self):
        bs, ms = degree("本科"), degree("硕士")
        ev = evidence("张三是华南理工大学本科生。", [f"学校={SCHOOL}"])
        prior = judge.assess(bs, [ev])
        ms.raw_text += " 保研"
        routed = with_verified_education_context(ms, [prior])
        self.assertEqual(routed.entities["source_school_hint"], SCHOOL)
        self.assertEqual(routed.entities["verified_education_context"][0]["proved_elements"], [f"学校={SCHOOL}"])
        self.assertEqual(ms.entities, {"org": SCHOOL})

    def test_enrollment_is_not_award_and_needs_no_model_call(self):
        class NoModel:
            def complete_json(self, *a, **k):
                raise AssertionError("就读页面不得花获奖抽取调用")
        text = "张三是华南理工大学本科生，通过推免进入本校硕士阶段。"
        self.assertIsNone(extract_evidence(award(), SearchHit(URL, "推免录取名单"), text, NoModel(), candidate_name="张三"))

    def test_scholarship_policy_is_not_personal_award(self):
        text = "张三是华南理工大学本科生，符合国家奖学金申请条件。"
        self.assertIsNone(extract(award(), text, "奖学金申请说明"))

    def test_other_student_award_cannot_be_moved_to_candidate(self):
        text = "张四（本科国家奖学金获得者）；张三（本科生，在华南理工大学就读）。"
        self.assertFalse(award_scope_matches(award(), text, "导师主页", "张三"))
        self.assertIsNone(extract(award(), text, "导师主页"))

    def test_bachelor_award_does_not_use_master_or_doctor_award(self):
        for stage in ("硕士", "博士", "研究生"):
            text = f"2020年张三（{stage}）获得华南理工大学国家奖学金。"
            self.assertIsNone(extract(award(), text, "国家奖学金获得者介绍"))

    def test_award_name_school_year_and_final_stage_match(self):
        for text, title in [
            ("2020年张三本科阶段获得华南理工大学国家励志奖学金。", "获奖介绍"),
            ("2020年张三本科阶段获得哈尔滨工业大学国家奖学金。", "获奖介绍"),
            ("2021年张三本科阶段获得华南理工大学国家奖学金。", "获奖介绍"),
            ("2021年张三本科阶段获得华南理工大学国家奖学金。", "2020年国家奖学金获得者介绍"),
            ("张三 华南理工大学 本科 国家奖学金", "2020年本科国家奖学金拟推荐名单")]:
            self.assertIsNone(extract(award(), text, title))
        c = with_academic_scope_context([award()])[0]
        self.assertIn("获奖学段=本科", c.elements)
        self.assertIn("获奖年份=2020", c.elements)
        text = "2020年张三本科阶段获得华南理工大学国家奖学金。"
        self.assertEqual(judge.assess(c, [extract(c, text, "2020年本科国家奖学金获奖名单")]).status, "ok")

    def test_missing_award_year_is_partial_not_invented(self):
        c = with_academic_scope_context([award()])[0]
        ev = extract(c, "张三本科阶段获得华南理工大学国家奖学金。")
        self.assertNotIn("获奖年份=2020", ev.supports)
        self.assertEqual(judge.assess(c, [ev]).status, "part")

    def test_awards_do_not_borrow_enrollment_and_degrees_do_not_borrow_awards(self):
        bs, pd, a = degree("本科"), degree("博士"), award()
        b_ev = evidence("张三是华南理工大学博士生。", [f"学校={SCHOOL}"])
        a_ev = evidence("2020年张三本科阶段获得华南理工大学国家奖学金。", a.elements)
        vcs = [judge.assess(bs, []), judge.assess(pd, [b_ev]), judge.assess(a, [a_ev])]
        self.assertEqual(judge.corroborate_enrollment(vcs, "张三"), [])
        self.assertEqual(vcs[0].status, "none")
        invalid = evidence("张三是华南理工大学本科生。", a.elements)
        self.assertEqual(judge.assess(a, [invalid]).evidence, [])

    def test_award_plan_has_award_purpose_and_stage(self):
        plan = build_plan(award(), "张三", ResumeProfile(identity="researcher"))
        self.assertTrue(plan.queries)
        self.assertTrue(all("奖" in q.text or "优秀" in q.text for q in plan.queries))
        self.assertTrue(all("本科" in q.text for q in plan.queries if q.kind == "web"))

    def test_pipeline_runs_stages_in_order_and_keeps_report_order(self):
        claims = [award("博士", "2026"), degree("博士"), degree("本科"), degree("硕士")]
        started, finished = [], []
        def collect(c, *a, **kw):
            started.append(c.id)
            if c.id == "硕士":
                self.assertIn("本科", finished)
                self.assertEqual(c.entities["verified_education_context"][0]["stage"], "本科")
            if c.category != "学历":
                self.assertIn("博士", finished)
                finished.append(c.id)
                return [], False
            stage = study_stage(c)
            ev = evidence(f"张三是华南理工大学{stage}学生。", [f"学校={SCHOOL}"])
            finished.append(c.id)
            return [ev], False
        prep = SimpleNamespace(doc=SimpleNamespace(kind="text", source="", risks=[]), llm=object(),
                               profile=ResumeProfile(identity="researcher"), name="张三",
                               resume_path="合成测试", claims=claims, skipped=[])
        with patch.dict(os.environ, {"CLAIM_WORKERS": "0"}), patch.object(pipeline, "collect_for_claim", collect), \
             patch.object(pipeline, "generate_question", lambda *a: "待补证"):
            report = pipeline.verify_prepared(prep, searcher=object(), fetcher=object())
        self.assertEqual(started, ["本科", "硕士", "博士", "award"])
        self.assertEqual([v.claim.id for v in report.claims], [c.id for c in claims])

class UndergradOriginBoundaryTests(unittest.TestCase):
    def test_source_in_parentheses_is_not_receiving_school(self):
        source='张三 2021级硕士 工业工程专业保研（四川大学工管学院）'
        self.assertIsNone(extract(degree('本科'), source))
        source='张三 2021级硕士 工业工程专业保研（华工工管学院）'
        ev=extract(degree('本科','2017','2021'),source)
        self.assertIsNotNone(ev)
        self.assertNotIn('学位=学士',ev.supports)

    def test_bare_admission_destination_is_not_undergraduate_origin(self):
        self.assertIsNone(extract(degree('本科'),'张三 获得推免（华南理工大学）资格，随后进入2021级硕士。'))


class HonorRosterTests(unittest.TestCase):
    def claim(self, name="校级三好学生"):
        return Claim(id="honor", raw_text=f"2023年本科期间获{SCHOOL}{name}", raw_locator="合成测试",
                     category="校内荣誉", date_label="2023",
                     elements=[f"奖项={name}", f"授予单位={SCHOOL}", "获奖年份=2023"],
                     entities={"org": SCHOOL, "award": name})

    def test_old_three_good_student_and_outstanding_graduate_fixtures_are_confirmed(self):
        path = Path(__file__).resolve().parents[1] / "fixtures" / "report_lin.json"
        report = Report(**json.loads(path.read_text(encoding="utf-8")))
        for item in report.claims:
            if item.claim.id in {"c02", "c11"}:
                with self.subTest(id=item.claim.id):
                    result = judge.assess(item.claim, list(item.evidence))
                    self.assertEqual(result.status, "ok")
                    self.assertEqual(result.best_tier, "A")
                    self.assertEqual(result.proved, item.claim.elements)

    def test_named_honor_roster_supplies_award_year_not_enrollment_year(self):
        for name in ("校级三好学生", "校级优秀毕业生", "优秀学生干部", "学习标兵"):
            with self.subTest(name=name):
                c = self.claim(name)
                ev = extract(c, "张三 华南理工大学 本科 计算机科学 2021级",
                             f"关于公布2023年本科{name}名单的公示")
                self.assertIsNotNone(ev)
                self.assertIn("获奖年份=2023", ev.supports)
                self.assertEqual(judge.assess(c, [ev]).status, "ok")

    def test_named_honor_still_requires_year_person_and_study_stage(self):
        c = self.claim()
        for text, title in (
            ("张三 华南理工大学 本科 2021级", "2024年本科校级三好学生名单公示"),
            ("张四 华南理工大学 本科 2021级", "2023年本科校级三好学生名单公示"),
            ("张三 华南理工大学 硕士 2021级", "2023年硕士校级三好学生名单公示"),
            ("张三 华南理工大学 本科 2021级", "2023年本科校级优秀毕业生名单公示"),
        ):
            with self.subTest(text=text, title=title):
                self.assertIsNone(extract(c, text, title))

    def test_other_school_named_honor_is_not_candidate_evidence(self):
        c = self.claim()
        ev = Evidence(url="https://www.hit.edu.cn/honors/1.htm",
                      title="2023年本科校级三好学生名单公示", publisher="哈尔滨工业大学",
                      source_tier="B",
                      snippet="张三 哈尔滨工业大学 本科 计算机科学",
                      identity_signals=["学校一致", "专业一致"], supports=c.elements)
        self.assertEqual(judge.assess(c, [ev]).evidence, [])

    def test_application_candidates_enrollment_and_events_do_not_prove_honor(self):
        c = self.claim()
        text = "张三 华南理工大学 本科 2021级"
        for title in (
            "2023年本科校级三好学生申报名单", "2023年本科校级三好学生候选名单",
            "2023年本科校级三好学生评选办法与学生名单", "2023年本科校级三好学生录取名单",
            "2023年校级三好学生参加讲座名单", "2023年本科入学名单",
            "2023年本科校级三好学生拟推荐名单",
        ):
            with self.subTest(title=title):
                self.assertIsNone(extract(c, text, title))

    def test_missing_honor_year_is_partial_not_invented_from_grade(self):
        c = self.claim()
        ev = extract(c, "张三 华南理工大学 本科 2021级", "本科校级三好学生名单公示")
        self.assertIsNotNone(ev)
        self.assertNotIn("获奖年份=2023", ev.supports)
        self.assertEqual(judge.assess(c, [ev]).status, "part")

    def test_notice_dates_cannot_override_award_years_or_supply_missing_year(self):
        c = self.claim()
        text = "张三 华南理工大学 本科 2021级；公示期为2023年5月12日至5月18日。"
        self.assertIsNone(extract(c, text, "2022年本科校级三好学生名单公示"))
        ev = extract(c, text, "本科校级三好学生名单公示")
        self.assertIsNotNone(ev)
        self.assertNotIn("获奖年份=2023", ev.supports)
        self.assertEqual(judge.assess(c, [ev]).status, "part")


if __name__ == "__main__":
    unittest.main()
