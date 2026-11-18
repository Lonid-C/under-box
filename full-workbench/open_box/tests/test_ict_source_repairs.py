"""Checks for distinct attachment, source-access, and identity recovery paths."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
import pymupdf

from app.collect import Budget, _read_and_extract
from app.ocr import OCRText
from app.public_documents import legacy_office_read
from app.schema import Claim
from app.search import PageFetcher, SearchHit


class SourceRepairTests(unittest.TestCase):
    def setUp(self):
        config = patch.dict(os.environ, {"UNDERBOX_ICT_SOURCE_REPAIR": "1", "UNDERBOX_ICT_RECALL": "1",
                                        "UNDERBOX_ICT_UNLIMITED": "0", "DEEPSEEK_API_KEY": "synthetic"})
        config.start()
        self.addCleanup(config.stop)

    def test_word_parser_uses_scoped_resource_home_and_reports_missing_mapping(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "UNDERBOX_ICT_DOC_HOME": directory, "UNDERBOX_ICT_DOC_READER": "/test/antiword"
        }):
            data = b"\xd0\xcf\x11\xe0" + b"synthetic"
            self.assertIn("字符映射", legacy_office_read(data)[1])
            path = Path(directory) / ".antiword"
            path.mkdir()
            (path / "UTF-8.txt").write_text("synthetic")
            def invoke(command, **kwargs):
                self.assertEqual(kwargs["env"]["HOME"], directory)
                self.assertEqual(kwargs["env"]["ANTIWORDHOME"], str(path))
                kwargs["stdout"].write("合成名单".encode())
                return SimpleNamespace(returncode=0)
            with patch("app.public_documents.subprocess.run", side_effect=invoke):
                self.assertEqual(legacy_office_read(data), ("合成名单", ""))

    def test_same_scan_at_two_urls_uses_ocr_once_and_retains_ocr_type(self):
        with pymupdf.open() as doc:
            page = doc.new_page()
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 4, 4), False)
            pix.clear_with(127)
            page.insert_image(pymupdf.Rect(0, 0, 80, 80), pixmap=pix)
            data = doc.tobytes()
        response = httpx.Response(200, content=data, request=httpx.Request("GET", "https://school.example/a.pdf"))
        fetcher = PageFetcher()
        with patch("app.search.robots_allows", return_value=True), patch.object(fetcher, "_throttle"), \
             patch("app.safe_http.public_request", return_value=response), \
             patch("app.deepseek_client.DeepSeekClient.ocr_bytes", return_value=[(1, "张示例参加合成项目")]) as ocr:
            first = fetcher.get("https://school.example/a.pdf")
            second = fetcher.get("https://mirror.example/a.pdf")
        self.assertIsInstance(first, OCRText)
        self.assertEqual(first, second)
        self.assertEqual(ocr.call_count, 1)
        self.assertIn("张示例", first)

    def test_ocr_reader_marker_reaches_extraction(self):
        claim = Claim(id="example", category="项目", raw_text="张示例参加合成项目", raw_locator="", date_label="")
        url = "https://school.example/roster.pdf"
        page = OCRText("合成项目张示例参加记录")
        with patch("app.collect.extract_evidence", return_value=None) as extract:
            _read_and_extract(claim, [SearchHit(url, title="[赛事目录入口] 合成记录", content=page)],
                              SimpleNamespace(), object(), set(), candidate_name="张示例")
        self.assertIn("名单图片OCR识别", extract.call_args.args[1].title)

    def test_only_https_same_school_relocation_is_followed_for_tls_failure(self):
        claim = Claim(id="example", category="奖学金", raw_text="清华大学合成奖学金", raw_locator="", date_label="",
                      entities={"school": "清华大学"})
        title = "合成奖学金获奖名单公示"
        old = "https://old.tsinghua.edu.cn/original"
        valid = "https://new.tsinghua.edu.cn/record"
        read = Mock(side_effect=lambda url: "张示例 合成奖学金" if url == valid else None)
        fetcher = SimpleNamespace(get=read, failures={old: "证书校验失败（SSL）"}, resolved_urls={})
        search = Mock(return_value=[SearchHit("http://old.tsinghua.edu.cn/record", title),
                        SearchHit("https://old.tsinghua.edu.cn/another", title),
                        SearchHit("https://other.example/record", title), SearchHit(valid, title)])
        budget = Budget()
        budget.note_read()
        with patch("app.search.robots_allows", return_value=True), patch("app.collect.extract_evidence", return_value=None):
            _read_and_extract(claim, [SearchHit(old, title)], fetcher, object(), set(), candidate_name="张示例",
                              budget=budget, searcher=SimpleNamespace(search=search))
        self.assertEqual([call.args[0] for call in read.call_args_list], [old, valid])
        search.assert_called_once_with(f'"{title}"', site="tsinghua.edu.cn")

    def test_blocked_paper_uses_only_exact_title_from_doi_registry(self):
        title = "Synthetic publisher manuscript"
        url = "https://publisher.example/doi/10.1234/example"
        c = Claim(id="c1", category="论文", raw_text=title, raw_locator="", date_label="", entities={"title": title})
        f = SimpleNamespace(get=lambda _: None, failures={url: "HTTP 403"}, resolved_urls={})
        def run(metadata_title):
            h = SearchHit("https://doi.org/10.1234/example", title=metadata_title,
                          content="public registry record", author_records=({"name": "Lin Example"},))
            with patch("app.search.crossref_doi_hit", return_value=h) as lookup, \
                 patch("app.collect.extract_evidence", return_value=None) as extract:
                _read_and_extract(c, [SearchHit(url, title)], f, object(), set(), budget=Budget())
            lookup.assert_called_once_with("10.1234/example")
            return extract.call_count
        self.assertEqual(run(title), 1)
        self.assertEqual(run("Different paper title"), 0)
