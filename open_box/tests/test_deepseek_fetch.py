import unittest
from unittest.mock import patch
from app.deepseek_fetch import DeepSeekPageFetcher, _html_from_markdown
from app.deepseek_client import DeepSeekError

class FetchTests(unittest.TestCase):
    def test_markdown_links_preserved_and_markup_escaped(self):
        result = _html_from_markdown('名单 [附件](/list.pdf) <script>alert(1)</script>')
        self.assertIn('href="/list.pdf"', result)
        self.assertNotIn('<script>', result)
        heading = _html_from_markdown('# 数据科学学院\n专业介绍')
        self.assertIn('<title>数据科学学院</title>', heading)
        self.assertIn('<h1>数据科学学院</h1>', heading)

    def test_public_web_uses_fetch_and_keeps_links(self):
        f = DeepSeekPageFetcher()
        with patch("app.deepseek_fetch.robots_allows", return_value=True), \
             patch.object(f, "_throttle"), \
             patch("app.deepseek_fetch.DeepSeekClient") as client:
            client.return_value.fetch.return_value = '公示 [名单](https://school.example/list.pdf)'
            result = f.get("https://school.example/info")
        self.assertIn('href="https://school.example/list.pdf"', result)
        self.assertEqual(f.resolved_urls["https://school.example/info"], "https://school.example/info")

    def test_failure_falls_back_to_original_fetcher(self):
        f = DeepSeekPageFetcher()
        with patch("app.deepseek_fetch.robots_allows", return_value=True), \
             patch.object(f, "_throttle"), \
             patch("app.deepseek_fetch.DeepSeekClient") as client, \
             patch("app.search.PageFetcher.get", return_value="fallback") as old:
            client.return_value.fetch.side_effect = DeepSeekError("fetch")
            self.assertEqual(f.get("https://school.example/info"), "fallback")
            old.assert_called_once()

    def test_security_rejection_does_not_bypass_policy(self):
        f=DeepSeekPageFetcher()
        with patch("app.deepseek_fetch.robots_allows", return_value=True), \
             patch.object(f, "_throttle"), patch("app.deepseek_fetch.DeepSeekClient") as client, \
             patch("app.search.PageFetcher.get") as old:
            client.return_value.fetch.side_effect=DeepSeekError("fetch",403)
            self.assertIsNone(f.get("https://school.example/info"))
            old.assert_not_called()

    def test_local_document_is_never_fetched(self):
        f = DeepSeekPageFetcher()
        with patch("app.search.PageFetcher.get") as old:
            self.assertIsNone(f.get("http://127.0.0.1/private.pdf"))
            old.assert_not_called()


class RosterImageTests(unittest.TestCase):
    def test_vsb_images_on_school_results_page_are_roster_candidates(self):
        from app.competitions import roster_image_links
        from app.search import SearchHit
        from app.schema import Claim
        claim = Claim(id="x", category="竞赛", raw_text="2026 MCM 数学建模竞赛",
                      raw_locator="第1页", date_label="2026",
                      elements=["赛事=MCM"], entities={"contest":"MCM","school":"北师香港浸会大学"})
        page = SearchHit("https://www.bnbu.edu.cn/info/1111/117819.htm", title="数模竞赛")
        html = ('<div class="v_news_content"><p>2026 年获奖名单</p>'
                '<img src="/virtual_attach_file.vsb?afc=abc">'
                '<img src="https://other.example/virtual_attach_file.vsb?afc=abc"></div>')
        items = roster_image_links(html, page, claim)
        self.assertEqual(len(items), 1)
        self.assertIn("名单图片OCR识别", items[0]["title"])
        self.assertTrue(items[0]["url"].startswith("https://www.bnbu.edu.cn/"))

    def test_school_roster_keeps_original_html(self):
        from app.schema import Claim
        f = DeepSeekPageFetcher()
        claim = Claim(id="x", category="竞赛", raw_text="MCM",
                      raw_locator="第1页", date_label="2026", elements=[], entities={})
        with patch("app.search.PageFetcher.get", return_value="<html>raw images</html>") as old:
            result = f.get_roster("https://school.example/a", claim)
        self.assertIn("raw images", result)
        old.assert_called_once()

class CompetitionQueryTests(unittest.TestCase):
    def test_mcm_school_query_uses_relaxed_search_terms(self):
        from app.competitions import competition_for_claim, catalog_query_specs
        from app.schema import Claim
        claim = Claim(id="x", category="竞赛", raw_text="2026 MCM 数学建模",
                      raw_locator="第1页", date_label="2026",
                      elements=["赛事=MCM"], entities={"contest":"MCM","school":"北师香港浸会大学"})
        entries = catalog_query_specs(competition_for_claim(claim), claim, "候选人")
        school = next(q for q in entries if q["site"]=="bnbu.edu.cn" and "catalog-roster" in q["source"])
        self.assertIn("数学建模 美赛",school["text"])
        self.assertNotIn('"MCM ICM"',school["text"])

class OCRSafetyTests(unittest.TestCase):
    def test_public_roster_image_uses_deepseek_ocr(self):
        import os
        from app.ocr import recognize_image
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test"}), \
             patch("app.deepseek_client.DeepSeekClient.ocr_bytes", return_value=[(1,"张三 获奖")]) as ocr:
            result = recognize_image(b"\x89PNG\r\n\x1a\nexample")
        self.assertIn("张三 获奖", result)
        ocr.assert_called_once()

    def test_empty_ocr_is_not_treated_as_readable(self):
        import os
        from app.ocr import recognize_image
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test"}), \
             patch("app.deepseek_client.DeepSeekClient.ocr_bytes", return_value=[(1,"")]), \
             patch("app.ocr._vision_binary", return_value=None):
            self.assertIsNone(recognize_image(b"\x89PNG\r\n\x1a\nexample"))

    def test_ocr_alone_never_yields_confirmed(self):
        from app.judge import decide
        from app.schema import Claim, Evidence
        c=Claim(id="x",category="竞赛",raw_text="张三获奖",raw_locator="第1页",
                date_label="2026",elements=["奖项=一等奖"],entities={})
        e=Evidence(url="https://school.example/list.png",title="获奖名单",publisher="学校",
                   source_tier="A",snippet="张三获一等奖",identity_score=4,
                   supports=["奖项=一等奖"],extraction_method="ocr")
        self.assertEqual(decide(c,[e]),"part")

if __name__ == "__main__": unittest.main()
