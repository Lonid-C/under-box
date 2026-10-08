"""科研成果识别（论文 / 专利 / 学术会议）与实习不核验。"""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app import pipeline, search
from app.schema import Claim
from app.search import SearchHit
from app.split import add_research_ids, split_claims
from app.strategy import ResumeProfile, build_plan

RESUME = Path(__file__).resolve().parent.parent / "fixtures" / "resume_lin.md"
PROFILE = ResumeProfile(identity="student", name="某候选人")


def mk(cat, text, elements=(), **entities):
    return Claim(id="c01", raw_text=text, raw_locator="第1页", category=cat, date_label="2024",
                 date_start="2024-05-01", elements=list(elements), entities=entities)


class _SplitLLM:
    def __init__(self, items):
        self.items = items

    def complete_json(self, *a, **k):
        return self.items


class ResearchIdTests(unittest.TestCase):
    def test_ids_are_read_from_raw_text_when_the_model_missed_them(self):
        cases = [
            ("论文", "A Sparse Retriever. ACL 2024. DOI: 10.18653/v1/2024.acl-long.12。", "doi",
             "10.18653/v1/2024.acl-long.12"),
            ("论文", "预印本 arXiv:2403.01234v2，第一作者", "arxiv", "2403.01234"),
            ("专利", "一种图像去噪方法，发明专利，申请号 202110123456.7", "patent_no", "202110123456.7"),
            ("专利", "授权公告号 CN 114123456 B，第二发明人", "patent_no", "CN114123456B"),
            ("专利", "实用新型 ZL202220123456.X", "patent_no", "ZL202220123456.X"),
            ("专利", "软件著作权 智能排课系统，登记号 2023SR0123456", "patent_no", "2023SR0123456"),
            ("专利", "US 11,234,567 B2", "patent_no", "US11234567B2"),
        ]
        for cat, text, key, want in cases:
            with self.subTest(text=text):
                self.assertEqual(add_research_ids([mk(cat, text)])[0].entities.get(key), want)

    def test_model_supplied_ids_are_kept(self):
        c = add_research_ids([mk("专利", "专利号 CN114123456B", patent_no="CN 114123456 B")])[0]
        self.assertEqual(c.entities["patent_no"], "CN 114123456 B")

    def test_ids_do_not_add_elements(self):
        c = add_research_ids([mk("论文", "DOI 10.1000/xyz123", ["论文标题=某论文"])])[0]
        self.assertEqual(c.elements, ["论文标题=某论文"])     # 只作检索线索，不新增待证明要素

    def test_fake_dois_and_empty_elements_are_cleaned(self):
        c = add_research_ids([mk("论文", "某论文，计算机研究与发展，2013，EI 收录",
                                 ["论文标题=某论文", "DOI=EI:20990000000001", "arXiv=无", "收录=EI"],
                                 doi="EI:20990000000001")])[0]
        self.assertNotIn("doi", c.entities)                         # 不是 10. 开头的不当 DOI 查
        self.assertEqual(c.elements, ["论文标题=某论文", "DOI=EI:20990000000001", "收录=EI"])

    def test_internship_filed_as_employment_is_moved_back(self):
        moved = add_research_ids([mk("任职", "2024.06–2024.09 某科技公司 算法实习生"),
                                  mk("任职", "某科技公司 研究员", role="Research Intern"),
                                  mk("任职", "某科技公司 算法工程师")])
        self.assertEqual([c.category for c in moved], ["实习", "实习", "任职"])

    def test_other_json_keys_and_section_names_still_split(self):
        from app.split import LAST_SPLIT
        llm = _SplitLLM({"陈述": [{"raw_text": "某论文", "category": "发表论文"},
                                  {"raw_text": "某基金项目", "category": "国家级科研项目"},
                                  {"raw_text": "讲授数据结构", "category": "讲授课程"}]})
        claims = split_claims("简历", llm)
        self.assertEqual([c.category for c in claims], ["论文", "项目"])
        self.assertEqual(LAST_SPLIT["given"], 3)
        self.assertEqual(LAST_SPLIT["dropped"], ["讲授课程（category 不合规）"])

    def test_split_accepts_conference_category(self):
        llm = _SplitLLM([{"id": "c01", "raw_text": "CNCC 2024 墙报", "category": "会议",
                          "elements": ["会议名称=中国计算机大会（CNCC 2024）", "参与形式=墙报"],
                          "entities": {"conference": "中国计算机大会（CNCC 2024）"}},
                         "不是对象的脏数据"])
        claims = split_claims("简历", llm)
        self.assertEqual([c.category for c in claims], ["会议"])


class ResumeGateTests(unittest.TestCase):
    def test_research_headings_count_as_resume_sections(self):
        from app.parse import ParsedDocument
        from app.resume_gate import ensure_resume

        class NoLLM:
            def complete_json(self, *a, **k):
                raise AssertionError("结构已能确认是简历，不该再问模型")

        doc = ParsedDocument("# 林某\nlin@example.com\n## 科研成果\n某论文，第一作者\n"
                             "## 专利与软著\n一种方法，发明专利\n## 学术会议\nCNCC 2024 墙报\n",
                             [], "x.md", "text")
        ensure_resume(doc, NoLLM())
        self.assertTrue(doc.resume_validated)


class ResearchPlanTests(unittest.TestCase):
    def queries(self, claim):
        return build_plan(claim, "某候选人", PROFILE, resume_categories=["学历", claim.category]).queries

    def test_patent_number_is_the_first_query_in_the_patent_database(self):
        qs = self.queries(mk("专利", "一种图像去噪方法，授权公告号 CN114123456B",
                             ["专利名称=一种图像去噪方法", "专利号=CN114123456B"],
                             title="一种图像去噪方法", patent_no="CN114123456B"))
        self.assertEqual((qs[0].kind, qs[0].text), ("patent", "CN114123456B"))
        self.assertIn('"CN114123456B"', [q.text for q in qs])

    def test_software_copyright_skips_patent_database_queries(self):
        qs = self.queries(mk("专利", "软件著作权：智能排课系统，登记号 2023SR0123456",
                             ["软件名称=智能排课系统", "登记号=2023SR0123456"],
                             title="智能排课系统", patent_no="2023SR0123456"))
        self.assertFalse([q for q in qs if q.kind == "patent"])
        self.assertFalse([q for q in qs if "专利" in q.text or "申请人" in q.text])
        self.assertIn('"智能排课系统" 软件著作权 登记', [q.text for q in qs])

    def test_conference_uses_title_and_both_name_forms(self):
        texts = [q.text for q in self.queries(mk(
            "会议", "中国计算机大会（CNCC 2024）墙报：基于稀疏注意力的检索",
            ["会议名称=中国计算机大会（CNCC 2024）", "报告题目=基于稀疏注意力的检索"],
            conference="中国计算机大会（CNCC 2024）", title="基于稀疏注意力的检索"))]
        self.assertEqual(texts[0], '"基于稀疏注意力的检索" "某候选人"')
        self.assertIn('"中国计算机大会" "某候选人"', texts)
        self.assertIn('"CNCC 2024" "某候选人"', texts)

    def test_conference_name_falls_back_to_elements(self):
        texts = [q.text for q in self.queries(mk(
            "会议", "参加 NeurIPS 2023 并作口头报告", ["会议名称=NeurIPS 2023", "参与形式=口头报告"]))]
        self.assertIn('"NeurIPS 2023" "某候选人"', texts)

    def test_paper_doi_query_is_planned(self):
        texts = [q.text for q in self.queries(mk(
            "论文", "A Sparse Retriever. DOI 10.18653/v1/2024.acl-long.12",
            ["论文标题=A Sparse Retriever"], title="A Sparse Retriever",
            doi="10.18653/v1/2024.acl-long.12"))]
        self.assertIn('"10.18653/v1/2024.acl-long.12"', texts)


class DoiLookupTests(unittest.TestCase):
    def test_doi_record_is_used_only_when_its_title_matches(self):
        right = SearchHit(url="https://doi.org/10.1/a", title="A Sparse Retriever")
        wrong = SearchHit(url="https://doi.org/10.1/b", title="Something Else Entirely")
        with patch.object(search, "crossref_hits", lambda *a, **k: []), \
             patch.object(search, "openalex_hits", lambda *a, **k: []):
            with patch.object(search, "crossref_doi_hit", lambda doi: right):
                hits = search.paper_hits("A Sparse Retriever", doi="10.1/a")
            self.assertEqual([h.url for h in hits], ["https://doi.org/10.1/a"])
            with patch.object(search, "crossref_doi_hit", lambda doi: wrong):
                self.assertEqual(search.paper_hits("A Sparse Retriever", doi="10.1/b"), [])

    def test_bad_doi_is_not_requested(self):
        self.assertIsNone(search.crossref_doi_hit("not-a-doi"))


class SkipInternshipTests(unittest.TestCase):
    def _claims(self):
        return [mk("论文", "某论文", ["论文标题=某论文"], title="某论文").model_copy(update={"id": "c01"}),
                mk("实习", "2024.06–2024.09 某科技公司 算法实习生").model_copy(update={"id": "c02"}),
                mk("专利", "一种方法", ["专利名称=一种方法"], title="一种方法").model_copy(update={"id": "c03"})]

    def _prepare(self, env):
        logs = []
        with patch.dict(os.environ, env), \
             patch.object(pipeline, "ensure_resume", lambda d, l: None), \
             patch.object(pipeline, "derive_profile", lambda t, l: PROFILE), \
             patch.object(pipeline, "split_claims", lambda t, l: self._claims()):
            prepared = pipeline.prepare_resume(RESUME, llm=object(), progress=logs.append)
        return prepared, logs

    def test_internships_are_split_but_not_verified_by_default(self):
        os.environ.pop("SKIP_CATEGORIES", None)
        prepared, logs = self._prepare({})
        self.assertEqual([c.id for c in prepared.claims], ["c01", "c03"])
        self.assertEqual([c.id for c in prepared.skipped], ["c02"])
        self.assertTrue(any("实习 1 条按设置不核验" in line for line in logs), logs)

        with patch.object(pipeline, "collect_for_claim", lambda *a, **k: ([], False)), \
             patch.object(pipeline, "generate_question", lambda vc, l: "问题"):
            report = pipeline.verify_prepared(prepared, searcher=object(), fetcher=object())
        self.assertEqual([v.claim.id for v in report.claims], ["c01", "c03"])
        self.assertEqual([c.id for c in report.skipped_claims], ["c02"])

    def test_skip_list_can_be_turned_off(self):
        prepared, _ = self._prepare({"SKIP_CATEGORIES": "-"})
        self.assertEqual(len(prepared.claims), 3)
        self.assertEqual(prepared.skipped, [])


if __name__ == "__main__":
    unittest.main()
