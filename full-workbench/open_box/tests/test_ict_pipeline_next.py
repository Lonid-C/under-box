"""Offline checks for multi-specialist scheduling and source failure recovery."""
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from app import collect, ict_pipeline, pipeline
from app.schema import Claim, Evidence
from app.search import PageFetcher, SearchHit, ZhipuSearcher
from app.strategy import Plan, Query, ResumeProfile


def claim(identifier, category, text, **kwargs):
    return Claim(id=identifier, category=category, raw_text=text, raw_locator="",
                 date_label="", **kwargs)


class PipelineNextTests(unittest.TestCase):
    def setUp(self):
        self.use(patch.dict(os.environ, {"UNDERBOX_ICT_PIPELINE_NEXT": "1",
            "UNDERBOX_ICT_RECALL": "1", "UNDERBOX_ICT_UNLIMITED": "0"}))

    def use(self, context):
        result = context.start()
        self.addCleanup(context.stop)
        return result

    def test_patent_runs_during_education_and_affiliation_waits_for_works(self):
        claims = [
            claim("undergrad", "学历", "合成大学本科"),
            claim("graduate", "学历", "合成大学硕士"),
            claim("job", "任职", "合成大学讲师"),
            claim("paper", "论文", "合成论文甲"),
            claim("patent", "专利", "合成公开专利甲"),
        ]
        both_started = threading.Barrier(2, timeout=3)
        finished = set()
        assigned = []

        def verify(_, item):
            if item.id in {"undergrad", "patent"}:
                both_started.wait()
            return item.id

        def dispatch(item):
            if item.id == "graduate":
                assert "undergrad" in finished
            if item.id == "job":
                assert "paper" in finished
            assigned.append(item.id)
            return item

        ict_pipeline.run_agents(claims, 2, verify, finished.add, dispatch)
        assert assigned[:2] == ["undergrad", "patent"]
        assert finished == {item.id for item in claims}


    def test_failed_specialist_does_not_hide_other_claims_or_abort_report(self):
        self.use(patch.dict(os.environ, {"CLAIM_WORKERS": "2"}))
        claims = [claim("exchange", "交流活动", "2025年合成大学访学"),
                  claim("patent", "专利", "合成专利名称")]
        parsed = SimpleNamespace(kind="txt", source="fake", safe_text="", risks=[])
        prepared = pipeline.PreparedResume("fake", parsed, object(), ResumeProfile(), claims, "张示例")

        def collector(item, *args, **kwargs):
            if item.id == "exchange":
                raise KeyError("synthetic_source")
            return [], False

        with patch.object(pipeline, "collect_for_claim", side_effect=collector), \
             patch.object(pipeline, "employer_policy", return_value={}), \
             patch.object(pipeline, "plan_with_notes", return_value=Plan([Query("测试查询")], "test", "测试")), \
             patch.object(pipeline, "generate_question", return_value=None), \
             patch("app.contact_fallback.attach_contact_reviews"):
            report = pipeline.verify_prepared(prepared, searcher=SimpleNamespace(), fetcher=SimpleNamespace())
        failed, other = report.claims
        assert [row.claim.id for row in report.claims] == ["exchange", "patent"]
        assert failed.retrieval_status == "provider_error"
        assert failed.verification_agent["sourceAccess"]["execution"]["reason"] == "KeyError"
        assert "KeyError" in failed.next_step
        assert other.verification_agent["id"] == "patent"

    def test_production_mode_overlaps_four_glm_requests_without_enabling_experimental_limits(self):
        self.use(patch.dict(os.environ, {"UNDERBOX_ICT_PIPELINE_NEXT": "0",
            "UNDERBOX_ICT_PARALLEL_AGENTS": "1", "UNDERBOX_ICT_CLAIM_WORKERS": "4",
            "UNDERBOX_ICT_UNLIMITED": "1", "CLAIM_WORKERS": "2", "UNDERBOX_ICT_GLM_ONLY": "1"}))
        claims = [claim("school", "学历", "合成大学本科"),
                  claim("award", "奖学金", "合成奖学金"),
                  claim("paper", "论文", "合成论文甲"),
                  claim("patent", "专利", "合成公开专利甲")]
        doc = SimpleNamespace(kind="txt", source="fake", safe_text="", risks=[])
        prepared = pipeline.PreparedResume("fake", doc, object(), ResumeProfile(), claims, "张示例")
        pending = threading.Barrier(4, timeout=6)
        response = Mock(status_code=200)
        response.json.return_value = {"search_result": []}

        def fake_provider(*args, **kwargs):
            pending.wait()
            return response

        def plan(item, *args):
            return Plan([Query(f"{item.id} 合成查询")], "test", "测试",
                        budget={"searches": 1, "reads": 1, "seconds": 1})

        def verify():
            return pipeline.verify_prepared(prepared, searcher=ZhipuSearcher(api_key="synthetic"),
                                            fetcher=SimpleNamespace(get=lambda url: None))

        with patch.object(pipeline, "employer_policy", return_value={}), \
             patch.object(pipeline, "plan_with_notes", side_effect=plan), \
             patch.object(pipeline, "generate_question", return_value=None), \
             patch("app.education_review.build_education_review", return_value={}), \
             patch("app.contact_fallback.attach_contact_reviews"), \
             patch("httpx.post", side_effect=fake_provider) as network:
            report = verify()
        self.assertEqual(network.call_count, 4)
        self.assertEqual([v.claim.id for v in report.claims], [c.id for c in claims])
        for v in report.claims:
            self.assertEqual(v.retrieval_status, "complete")
            stats = v.verification_agent["sourceAccess"]
            self.assertEqual(stats["counts"]["search"]["empty"], 1)
            self.assertIsNone(stats["budget"]["maxSeconds"])


    def test_unreachable_origin_is_not_retried_and_other_school_host_still_works(self):
        fetcher = PageFetcher(timeout=6, connect_timeout=3)
        self.use(patch("app.search.robots_allows", return_value=True))
        self.use(patch.object(fetcher, "_throttle"))
        calls = []

        def read(method, url, **kwargs):
            calls.append(url)
            if "old." in url:
                raise httpx.ConnectError("[Errno 101] Network is unreachable")
            return httpx.Response(200, text="公开公告内容", request=httpx.Request(method, url))

        self.use(patch("app.safe_http.public_request", side_effect=read))
        assert fetcher.get("https://old.tsinghua.edu.cn/a") is None
        assert fetcher.get("https://old.tsinghua.edu.cn/b") is None
        assert "网络不可达" in fetcher.failures["https://old.tsinghua.edu.cn/a"]
        assert "已跳过" in fetcher.failures["https://old.tsinghua.edu.cn/b"]
        assert fetcher.get("https://new.tsinghua.edu.cn/a") == "公开公告内容"
        assert len(calls) == 2


    def test_school_award_recovers_same_notice_from_second_official_host(self):
        old = "https://old.tsinghua.edu.cn/awards/a"
        new = "https://new.tsinghua.edu.cn/awards/a"
        wrong = "https://unrelated.example/awards/a"
        title = "2025年合成奖学金获奖公示"
        item = claim("a", "奖学金", "清华大学2025年合成奖学金", entities={"school": "清华大学"})
        calls = []
        def read(url):
            calls.append(url)
            return "张示例获得合成奖学金" if url == new else None

        fetcher = SimpleNamespace(get=read, failures={old: "服务器到源站网络不可达"}, resolved_urls={})
        search_calls = []
        def search(query, site=None):
            search_calls.append((query, site))
            return [SearchHit(wrong, title=title), SearchHit(new, title=title)]

        searcher = SimpleNamespace(search=search)
        observed = []
        token = collect.EVIDENCE_OBSERVER.set(lambda c, ev: observed.append((ev.url, ev.extraction_method)))
        try:
            with patch.object(collect, "_official_roster_links", return_value=[]), \
                 patch("app.retrieval_recovery.school_record_links", return_value=[]), \
                 patch.object(collect, "extract_evidence", side_effect=lambda c, hit, body, *a, **k:
                     Evidence(url=hit.url, title=hit.title, snippet=body, publisher="", source_tier="A")):
                budget = collect.Budget(max_searches=6, max_page_reads=8)
                budget.note_read()
                results = collect._read_and_extract(item, [SearchHit(old, title=title, snippet="张示例")],
                    fetcher, object(), set(), candidate_name="张示例", budget=budget, searcher=searcher)
        finally:
            collect.EVIDENCE_OBSERVER.reset(token)
        assert calls == [old, new]
        assert search_calls == [(f'"{title}"', "tsinghua.edu.cn")]
        assert set(observed) == {(old, "search_snippet"), (new, "text")}
        assert next(ev for ev in results if ev.url == old).provenance_state != "page_text_verified"


    def test_long_resume_starts_with_unheaded_material_and_keeps_last_patent(self):
        first = "2022年合成大学本科"
        last = "一种合成题名的发明专利"
        text = first + "\n教育背景\n" + ("合成简历内容" * 450) + "\n专利\n" + last
        outputs = []

        class FakeLLM:
            def complete_json(self, system, prompt, **kwargs):
                assert "不超过 20 条" not in system
                outputs.append(prompt)
                rows = [claim("duplicate", cat, raw).model_dump() for raw, cat in
                        ((first, "学历"), (last, "专利"), ("模型新增内容", "论文")) if raw in prompt or cat == "论文"]
                return {"claims": rows}

        results = ict_pipeline.split_resume(text, FakeLLM())
        assert len(outputs) == 3
        assert [row.raw_text for row in results] == [first, last]
        assert [row.id for row in results] == ["c01", "c02"]

    def test_duplicate_model_rows_keep_one_original_and_grounded_fields(self):
        short = "示例大学学生会担任办公室副部长"
        full = "2025.06 在" + short + "，负责协调新生活动。"
        larger = claim("a", "学生工作", full, elements=["职务=办公室副部长"])
        repeated = claim("b", "学生工作", full, elements=["职责=负责协调新生活动", "职责=虚构职责"])
        fragment = claim("c", "学生工作", short, elements=["组织=示例大学学生会"])
        heading = claim("d", "项目", "项目经历")
        fake = Mock()
        fake.complete_json.return_value = {"claims": [r.model_dump() for r in (fragment, larger, repeated, heading)]}
        log = []
        results = ict_pipeline.split_resume("项目经历\n" + full, fake, progress=log.append)
        assert [item.raw_text for item in results] == [full]
        assert set(results[0].elements) == {"职务=办公室副部长", "职责=负责协调新生活动", "组织=示例大学学生会"}
        assert "合并 2 条" in log[0]

    def test_same_sentence_keeps_distinct_honor_but_normalizes_duplicate_scholarship(self):
        text = "2025年获示例大学「示例奖学金」及优秀志愿者称号。"
        scholarship = claim("a", "奖学金", text, elements=["奖项=示例奖学金", "学校=示例大学"])
        duplicate = claim("b", "校内荣誉", text, elements=["荣誉=示例奖学金"])
        honor = claim("c", "校内荣誉", text, elements=["荣誉=优秀志愿者"])
        fake = Mock()
        fake.complete_json.return_value = {"claims": [r.model_dump() for r in (duplicate, scholarship, honor)]}
        results = ict_pipeline.split_resume(text, fake)
        assert [(item.category, item.elements[0]) for item in results] == [
            ("奖学金", "奖项=示例奖学金"), ("校内荣誉", "荣誉=优秀志愿者")
        ]
        assert [item.raw_text for item in results] == [text, text]

    def test_competition_year_stage_and_award_disambiguate_shared_original(self):
        text = "示例挑战赛：2024校赛三等奖，2025校赛二等奖，2025省赛一等奖，2025省赛优秀奖。"
        scopes = [("2024", "校赛", "三等奖"), ("2025", "校赛", "二等奖"),
                  ("2025", "省赛", "一等奖"), ("2025", "省赛", "优秀奖")]
        rows = [claim(str(i), "竞赛", text, elements=["赛事=示例挑战赛", "年份="+year, "赛级="+level,
                                                    "奖级="+award]) for i, (year, level, award) in enumerate(scopes)]
        fake = Mock()
        fake.complete_json.return_value = {"claims": [r.model_dump() for r in rows]}
        assert [row.elements for row in ict_pipeline.split_resume(text, fake)] == [row.elements for row in rows]

    def test_long_split_matches_repeated_paper_by_doi_and_preserves_other_works(self):
        first = '研究说明收录《示例论文甲》，DOI 10.1234/synthetic.one'
        paper = '正式出版《示例论文甲》，DOI 10.1234/synthetic.one，刊物示例期刊。'
        different = '正式出版《示例论文乙》，DOI 10.1234/synthetic.two。'
        raw = f"科研成果\n{first}\n" + "测试填充。\n" * 420 + f"发表论文\n{paper}\n{different}"
        items = [claim(str(i), "论文", text, elements=[f"论文标题=示例论文{label}"])
                 for i, (text, label) in enumerate(((first, "甲"), (paper, "甲"), (different, "乙")))]
        fake = Mock()
        fake.complete_json.side_effect = lambda _, prompt, **kw: {
            "claims": [item.model_dump() for item in items if item.raw_text in prompt]
        }
        results = ict_pipeline.split_resume(raw, fake)
        assert [item.raw_text for item in results] == [paper, different]
        assert fake.complete_json.call_count == 2

    def test_separate_degrees_same_school_are_not_duplicates(self):
        text = "示例大学2018年就读本科，2022年继续就读硕士。"
        fake = Mock()
        items = [claim(str(i), "学历", text, elements=["学校=示例大学", "学段=" + stage])
                 for i, stage in enumerate(("本科", "硕士"))]
        fake.complete_json.return_value = {"claims": [row.model_dump() for row in items]}
        assert [row.elements for row in ict_pipeline.split_resume(text, fake)] == [row.elements for row in items]
