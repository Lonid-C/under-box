"""固定公开资料样例的漏检回归；不访问外网、不调用付费模型。"""
import unittest
from unittest.mock import patch

from app.collect import Budget, collect_for_claim, extract_evidence
from app.plan import Query, school_domain
from app.schema import Claim
from app.search import SearchHit, SearchUnavailable, ZhipuSearcher
from app.strategy import ResumeProfile, build_plan


def claim(**entities):
    return Claim(id="recall", raw_text="张三获一等奖", raw_locator="第1页",
                 category="竞赛", date_label="2024", elements=["奖项=一等奖"],
                 entities=entities)


class Extractor:
    def __init__(self):
        self.pages = []

    def complete_json(self, system, user, **kwargs):
        self.pages.append(user)
        return {"snippet": "张三获一等奖", "supports": ["奖项=一等奖"]}


class Fetcher:
    def __init__(self):
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        return "张三获一等奖"


class RecallTests(unittest.TestCase):
    def test_results_are_read_before_later_slow_queries(self):
        budget = Budget(max_searches=4, max_page_reads=4, max_seconds=10)
        reads = []

        class Search:
            def search(self, q, site=None):
                if q == "slow":
                    budget.started -= 20
                return [SearchHit("https://school.example/" + q, title="张三获一等奖")]

        class Fetch:
            def get(self, url):
                reads.append(url)
                return "张三获一等奖"

        found, _ = collect_for_claim(
            claim(), "张三", Search(), Extractor(), budget=budget, fetcher=Fetch(),
            queries=[Query("first"), Query("second"), Query("slow"), Query("last")])
        self.assertTrue(found, "后续慢查询不应抹掉已经取回的证据")
        self.assertIn("https://school.example/first", reads)

    def test_structured_content_does_not_require_display_page(self):
        fetcher = Fetcher()
        hit = SearchHit("https://github.com/example/demo", content="张三获一等奖")
        with patch("app.collect.github_hits", return_value=[hit]):
            found, _ = collect_for_claim(
                claim(repo="example/demo"), "张三", object(), Extractor(), queries=[],
                fetcher=fetcher, budget=Budget(max_searches=0, max_page_reads=2))
        self.assertEqual(len(found), 1)
        self.assertEqual(fetcher.calls, [])

    def test_long_notice_keeps_candidate_at_the_end(self):
        llm = Extractor()
        text = "名单说明\n" * 4000 + "张三获一等奖\n" + "名单结束"
        extract_evidence(claim(name="张三"), SearchHit("https://school.example/list"), text, llm)
        self.assertIn("张三获一等奖", llm.pages[0])

    def test_tracking_variants_do_not_consume_both_reads(self):
        class Search:
            def search(self, q, site=None):
                return [SearchHit("https://school.example/a?utm_source=one", title="张三"),
                        SearchHit("https://school.example/a?utm_source=two", title="张三"),
                        SearchHit("https://school.example/b", title="张三")]
        fetcher = Fetcher()
        collect_for_claim(claim(), "张三", Search(), Extractor(), fetcher=fetcher,
                          queries=[Query("奖项")], budget=Budget(max_page_reads=2))
        self.assertIn("https://school.example/b", fetcher.calls)

    def test_school_discovery_uses_existing_results_not_extra_requests(self):
        calls = []
        class Search:
            def search(self, q, site=None):
                calls.append((q, site))
                return [SearchHit("https://news.unknown.edu.cn/a", title="未知大学获奖公示")]
        collect_for_claim(claim(org="未知大学"), "张三", Search(), Extractor(),
                          queries=[Query("未知大学 公示", site="school")],
                          budget=Budget(max_searches=1, max_page_reads=0))
        self.assertEqual(calls, [("未知大学 公示", None)])

    def test_wrong_university_mention_does_not_lock_search_domain(self):
        calls = []
        class Search:
            def search(self, q, site=None):
                calls.append(site)
                return [SearchHit("https://other.edu.cn/a", title="其他大学访问新校大学新闻")]
        collect_for_claim(claim(org="新校大学"), "张三", Search(), Extractor(),
                          queries=[Query("新校大学 公示", site="school"),
                                   Query("新校大学 张三", site="school")],
                          budget=Budget(max_page_reads=0))
        self.assertEqual(calls, [None, None])

    def test_empty_exact_query_relaxes_within_budget(self):
        calls = []
        class Search:
            def search(self, q, site=None):
                calls.append(q)
                return [] if '"' in q else [SearchHit("https://school.example/result")]
        found, _ = collect_for_claim(
            claim(), "张三", Search(), Extractor(), fetcher=Fetcher(),
            queries=[Query('"张三" "一等奖"')], budget=Budget(max_searches=2))
        self.assertEqual(len(calls), 2)
        self.assertTrue(found)

    def test_full_school_name_beats_embedded_alias(self):
        self.assertEqual(school_domain("华南理工大学计算机学院"), "scut.edu.cn")
        self.assertIsNone(school_domain("南昌大学"))

    def test_variants_do_not_push_out_original_search_channels(self):
        c = claim(contest="示例竞赛（ABC）")
        doc = {"profiles": [{"id": "all", "match": {"is_fallback": True},
                             "budget": {"searches": 3}}],
               "categories": {"竞赛": {"forms": [
                   {"tpl": '"{contest}" "{name}"', "weight": 100},
                   {"tpl": '"{contest}" 公示', "weight": 90},
                   {"tpl": '"{contest}" "{name}"', "site": "wechat", "weight": 80}]}}}
        plan = build_plan(c, "张三 Sam Zhang", ResumeProfile(), doc=doc)
        self.assertTrue(any(q.site == "mp.weixin.qq.com" for q in plan.queries))

    def test_paper_dispatch_uses_free_apis_not_paid_search(self):
        """论文走 Crossref + OpenAlex（都免费），一次都不该碰计费搜索。"""
        class NoSearch:
            def search(self, q, site=None):
                raise AssertionError("论文陈述不该消耗搜索额度")

        with patch("app.collect.paper_hits", return_value=[SearchHit(
                "https://doi.org/test", content="张三获一等奖")]) as api:
            found, _ = collect_for_claim(
                claim(), "张三", NoSearch(), Extractor(), fetcher=Fetcher(),
                queries=[Query("Some paper", kind="crossref")])
        api.assert_called_once()
        self.assertTrue(found)

    def test_patent_queries_are_scoped_to_the_patent_domain_first(self):
        """专利不裸搜：裸查询走最贵的引擎，精度还最差。

        先在免费可读的专利域里找；那里没有才退回裸搜，且退回时仍是一次真实检索，
        不把"没查到"直接说成"不存在"。
        """
        from app.search import PATENT_SITE

        seen = []

        class Search:
            def search(self, q, site=None):
                seen.append(site)
                return []

        collect_for_claim(
            claim(), "张三", Search(), Extractor(), fetcher=Fetcher(),
            queries=[Query("\"张三\" 一种装置", kind="patent")],
            budget=Budget(max_searches=3, max_page_reads=1))

        self.assertEqual(seen[0], PATENT_SITE,
                         f"专利首查应限定到 {PATENT_SITE}，实际 {seen[0]}")
        self.assertIn(None, seen[1:], "专利域查空后应退回裸搜，不能就此了结")

    def test_provider_network_failure_is_not_an_empty_result(self):
        import httpx
        searcher = ZhipuSearcher(api_key="test", endpoint="https://search.example/query")
        with patch("httpx.post", side_effect=httpx.ReadTimeout("network unavailable")) as request, \
                patch("app.search.time.sleep"):
            with self.assertRaises(SearchUnavailable):
                searcher.search("公开获奖名单")
        self.assertEqual(request.call_count, 3)

    def test_canonical_urls_keep_wechat_article_identifiers(self):
        from app.collect import _url_key
        first = "https://mp.weixin.qq.com/s?__biz=abc&mid=1&idx=1&sn=x"
        second = "https://mp.weixin.qq.com/s?__biz=abc&mid=2&idx=1&sn=y"
        self.assertNotEqual(_url_key(first), _url_key(second))

    def test_page_failures_are_logged_separately_from_no_hits(self):
        logs = []
        class Search:
            def search(self, q, site=None):
                return [SearchHit("https://school.example/blocked")]
        fetcher = Fetcher()
        with patch.object(fetcher, "get", return_value=None):
            found, _ = collect_for_claim(
                claim(), "张三", Search(), Extractor(), queries=[Query("公示")],
                fetcher=fetcher, progress=logs.append)
        self.assertEqual(found, [])
        self.assertTrue(any("返回 1 条链接" in line for line in logs))
        self.assertTrue(any("失败/未完成 1" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
