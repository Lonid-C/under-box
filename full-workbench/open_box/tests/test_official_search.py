"""官网发现、备用索引与搜索并行的离线回归。"""
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.collect import (Budget, _discover_school_domain, _official_roster_links,
                         _read_and_extract, collect_for_claim)
from app.parse import extract_main_text
from app.plan import Query
from app.schema import Claim
from app.search import (BraveSearcher, FallbackSearcher, PageFetcher, SearchHit, SearchUnavailable,
                        crossref_hits, paper_hits)
from app.strategy import build_plan


class NoModel:
    def complete_json(self, *args, **kwargs):
        raise AssertionError("没有读页预算，不应调用模型")


class NoPages:
    def get(self, url):
        raise AssertionError("没有读页预算，不应抓取页面")


def make_claim(category, **entities):
    return Claim(id="official", raw_text="张三的简历陈述", raw_locator="第1页",
                 category=category, date_label="2024", elements=["成果=张三"],
                 entities=entities)


class OfficialSourceTests(unittest.TestCase):
    def test_push_notice_follows_only_official_pdf_roster_and_charges_read_budget(self):
        claim = Claim(id="push", raw_text="2023年保送至哈尔滨工业大学",
                      raw_locator="简历", category="学历", date_label="2023",
                      elements=["学校=哈尔滨工业大学", "入学方式=保送"],
                      entities={"org": "哈尔滨工业大学",
                                "source_school_hint": "哈尔滨工程大学"})
        notice = SearchHit("https://cstc.hrbeu.edu.cn/2022/0921/notice.htm",
                           title="计算机学院2023年推免资格名单公示",
                           publisher="哈尔滨工程大学计算机学院")
        pdf = "https://cstc.hrbeu.edu.cn/_upload/2023-list.pdf"
        html = (f'<main>名单见附件 <a href="{pdf}">附件：推免名单.pdf</a>'
                '<a href="https://not-hrbeu.example/list.pdf">附件：推免名单.pdf</a>'
                '</main>')
        self.assertEqual([h.url for h in _official_roster_links(html, notice, claim)], [pdf])

        class Pages:
            calls = []

            def get(self, url):
                self.calls.append(url)
                return {notice.url: html, pdf: "2023年推免名单\n李四 计算机科学与技术"}[url]

        pages = Pages()
        budget = Budget(max_searches=0, max_page_reads=2, max_seconds=30)
        budget.note_read()  # collect_for_claim 已给公告本身计数
        with patch("app.collect.extract_evidence", return_value=None) as extract:
            _read_and_extract(claim, [notice], pages, NoModel(), set(),
                              candidate_name="李四", budget=budget, seen_urls={notice.url})
        self.assertEqual(pages.calls, [notice.url, pdf])
        self.assertEqual(budget.page_reads, 2)
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(extract.call_args.args[1].url, pdf)

    def test_bilingual_club_uses_short_organization_names(self):
        claim = Claim(
            id="club", raw_text="Dream Weavers 思成筑梦大学生时间社团核心成员",
            raw_locator="第1页", category="学生工作", date_label="2024",
            elements=["任职组织=Dream Weavers 思成筑梦大学生时间社团", "职务=核心成员"],
            entities={"org": "Dream Weavers 思成筑梦大学生时间社团", "role": "核心成员"},
        )
        queries = build_plan(claim, "张三").queries
        self.assertEqual(queries[0].text, '"Dream Weavers" "思成筑梦"')
        self.assertIn('"Dream Weavers" "张三"', [q.text for q in queries])
        self.assertFalse(any("社团核心成员" in q.text for q in queries))

    def test_private_project_is_not_treated_as_open_source(self):
        claim = Claim(
            id="project", raw_text="大语言模型推理系统的构建", raw_locator="第1页",
            category="项目", date_label="2025", elements=["项目名称=大语言模型推理系统的构建"],
            entities={"title": "大语言模型推理系统的构建"},
        )
        queries = build_plan(claim, "张三").queries
        self.assertTrue(any(q.text == '"大语言模型推理系统的构建" "张三"' for q in queries))
        self.assertTrue(all(q.kind != "github" for q in queries))

    def test_page_fetcher_extracts_text_from_public_pdf_roster(self):
        sample = Path(__file__).resolve().parent.parent / "samples" / "resume_lin.pdf"
        response = SimpleNamespace(
            content=sample.read_bytes(), headers={"content-type": "application/pdf"},
            text="binary data", raise_for_status=lambda: None,
        )
        with patch("app.search.robots_allows", return_value=True), \
             patch("httpx.get", return_value=response):
            text = PageFetcher().get("https://school.example.edu/award-list.pdf")
        self.assertIn("林昱和", text)
        self.assertIn("晴川大学", text)

    def test_paper_page_keeps_authors_outside_abstract(self):
        html = ('<html><head><meta name="citation_title" content="Example Paper">'
                '<meta name="citation_author" content="Doe, Jane"></head>'
                '<body><header><h1>Example Paper</h1>'
                '<p class="paper-authors">Jane Doe</p></header>'
                '<main><h2>Abstract</h2><p>Research results.</p></main></body></html>')
        text = extract_main_text(html)
        self.assertIn("Example Paper", text)
        self.assertIn("Jane Doe", text)

    def test_planner_includes_each_relevant_official_source(self):
        education = build_plan(make_claim("学历", org="北京大学"), "张三")
        contest = build_plan(make_claim("竞赛", contest="全国算法大赛"), "张三")
        paper = build_plan(make_claim("论文", title="Novel Method", venue="Example Journal"), "张三")
        self.assertTrue(any(q.site == "pku.edu.cn" and q.source.startswith("school:")
                            for q in education.queries))
        self.assertTrue(any(q.site == "organizer" and q.source.startswith("organizer:")
                            for q in contest.queries))
        self.assertTrue(any(q.kind == "crossref" for q in paper.queries))
        self.assertTrue(any(q.site == "publisher" and q.source.startswith("publisher:")
                            for q in paper.queries))

    def test_explicit_english_school_is_not_skipped(self):
        claim = Claim(id="ucla", raw_text="Terence Tao, UCLA", raw_locator="第1页",
                      category="学历", date_label="1996", elements=["学校=UCLA"],
                      entities={"org": "UCLA"})
        plan = build_plan(claim, "Terence Tao")
        self.assertTrue(any(q.site == "school" and q.source.startswith("school:")
                            for q in plan.queries))
        domain = _discover_school_domain(
            "UCLA", [SearchHit("https://www.ucla.edu/",
                               title="A World Leader in Education and Research Excellence | UCLA")],
            homepage=True)
        self.assertEqual(domain, "ucla.edu")

    def test_known_conference_uses_its_proceedings_archive(self):
        neurips = build_plan(make_claim("论文", title="Attention Is All You Need",
                                        venue="NeurIPS"), "Ashish Vaswani")
        naacl = build_plan(make_claim("论文", title="BERT", venue="NAACL 2019"),
                           "Jacob Devlin")
        self.assertTrue(any(q.site == "proceedings.neurips.cc" for q in neurips.queries))
        self.assertTrue(any(q.site == "aclanthology.org" for q in naacl.queries))

    def test_known_icpc_contest_includes_official_news_subdomain(self):
        plan = build_plan(make_claim("竞赛", contest="ICPC World Finals 2024"),
                          "Wang Weicheng")
        self.assertTrue(any(q.site == "icpc.global" and q.source.startswith("organizer:")
                            for q in plan.queries))

    def test_contest_finds_organizer_homepage_then_scopes_name_search(self):
        calls = []

        class Search:
            def search(self, query, site=None):
                calls.append((query, site))
                if site is None:
                    return [SearchHit("https://contest.example.org/", title="全国算法大赛官方网站")]
                return [SearchHit("https://contest.example.org/winners", title="张三获奖名单")]

        cache = {}
        budget = Budget(max_searches=2, max_page_reads=0)
        collect_for_claim(
            make_claim("竞赛", contest="全国算法大赛"), "张三", Search(), NoModel(),
            queries=[Query('"张三" "全国算法大赛"', site="organizer", source="organizer:竞赛")],
            budget=budget, fetcher=NoPages(), organizer_domains=cache)
        self.assertEqual(calls[0], ('"全国算法大赛" 官方网站', None))
        self.assertEqual(calls[1], ('"张三" "全国算法大赛"', "contest.example.org"))
        self.assertEqual(cache, {"全国算法大赛": "contest.example.org"})
        self.assertEqual(budget.searches, 2)

    def test_paper_metadata_drives_publisher_site_search(self):
        calls = []

        class Search:
            def search(self, query, site=None):
                calls.append((query, site))
                return [SearchHit("https://journal.example.org/article/42", title="Novel Method")]

        metadata = SearchHit("https://doi.org/10.1/example", title="Novel Method",
                             content="作者：张三", official_url="https://journal.example.org/article/42")
        cache = {}
        with patch("app.collect.paper_hits", return_value=[metadata]):
            collect_for_claim(
                make_claim("论文", venue="Example Journal", venue_domain="wrong.example.org"),
                "张三", Search(), NoModel(), queries=[Query("Novel Method", kind="crossref")],
                budget=Budget(max_searches=2, max_page_reads=0), fetcher=NoPages(),
                publisher_domains=cache)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "journal.example.org")
        self.assertEqual(cache, {"Example Journal": "journal.example.org"})

    def test_same_title_wrong_author_does_not_select_publisher_domain(self):
        class NoSearch:
            def search(self, query, site=None):
                raise AssertionError("其他作者的同名论文不应触发该站检索")

        wrong = SearchHit("https://doi.org/10.1/other", title="Novel Method",
                          official_url="https://unrelated.example.org/paper",
                          authors=("Other Person",))
        with patch("app.collect.paper_hits", return_value=[wrong]):
            collect_for_claim(make_claim("论文"), "张三", NoSearch(), NoModel(),
                              queries=[Query("Novel Method", kind="crossref")],
                              budget=Budget(max_searches=1, max_page_reads=0),
                              fetcher=NoPages())

    def test_independent_queries_run_in_parallel_within_budget(self):
        barrier = threading.Barrier(2, timeout=2)
        calls = []

        class Search:
            def search(self, query, site=None):
                calls.append(query)
                barrier.wait()
                return []

        budget = Budget(max_searches=2, max_page_reads=2)
        collect_for_claim(make_claim("竞赛"), "张三", Search(), NoModel(),
                          queries=[Query("alpha"), Query("beta")],
                          budget=budget, fetcher=NoPages())
        self.assertEqual(set(calls), {"alpha", "beta"})
        self.assertEqual(budget.searches, 2)


class SearchProviderTests(unittest.TestCase):
    def test_crossref_uses_deposited_landing_page(self):
        def fake_get(url, **kwargs):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"message": {"items": [{
                    "DOI": "10.18653/v1/n19-1423", "title": ["BERT"],
                    "author": [{"given": "Jacob", "family": "Devlin"}],
                    "published": {"date-parts": [[2019, 6]]},
                    "resource": {"primary": {"URL": "https://aclanthology.org/N19-1423"}},
                }]}})

        with patch("httpx.get", side_effect=fake_get):
            hits = crossref_hits("BERT")
        self.assertEqual(hits[0].official_url, "https://aclanthology.org/N19-1423")
        self.assertEqual(hits[0].year, "2019")

    def test_same_title_different_years_are_not_merged(self):
        old = SearchHit("https://doi.org/10.1/old", title="Same Title",
                        authors=("Jane Doe",), year="2017")
        new = SearchHit("https://doi.org/10.1/new", title="Same Title",
                        authors=("Jane Doe",), year="2025")
        with patch("app.search.crossref_hits", return_value=[new, old]), \
                patch("app.search.openalex_hits", return_value=[]):
            hits = paper_hits("Same Title", author="Jane Doe", year="2017")
        self.assertEqual([h.url for h in hits], [old.url])

    def test_brave_uses_site_operator_and_filters_returned_hosts(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(
                status_code=200, raise_for_status=lambda: None,
                json=lambda: {"web": {"results": [
                    {"url": "https://news.pku.edu.cn/a", "title": "校内结果"},
                    {"url": "https://pku.edu.cn.evil.test/a", "title": "冒名结果"}]}})

        with patch("httpx.get", side_effect=fake_get), patch("app.search.RATE_LIMIT_SECONDS", 0):
            hits = BraveSearcher(api_key="test-key").search("张三 学位", site="pku.edu.cn")
        self.assertEqual(calls[0][1]["params"]["q"], "site:pku.edu.cn 张三 学位")
        self.assertEqual(calls[0][1]["headers"]["X-Subscription-Token"], "test-key")
        self.assertEqual([h.url for h in hits], ["https://news.pku.edu.cn/a"])

    def test_fallback_uses_second_index_after_empty_or_failure(self):
        class First:
            def __init__(self, fail=False):
                self.fail = fail

            def search(self, query, site=None):
                if self.fail:
                    raise SearchUnavailable("主索引无余额")
                return []

        class Second:
            def search(self, query, site=None):
                return [SearchHit("https://pku.edu.cn/notice")]

        for fail in (False, True):
            with self.subTest(fail=fail):
                self.assertEqual(len(FallbackSearcher(First(fail), Second()).search("张三")), 1)

    def test_paper_metadata_sources_are_called_concurrently(self):
        barrier = threading.Barrier(2, timeout=2)

        def source(title, limit=5):
            barrier.wait()
            return [SearchHit("https://doi.org/10.1/one", title="One")]

        with patch("app.search.crossref_hits", side_effect=source), \
                patch("app.search.openalex_hits", side_effect=source):
            self.assertEqual(len(paper_hits("One")), 1)


if __name__ == "__main__":
    unittest.main()
