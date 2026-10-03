"""文件内容识别和报告内检索复用；无需模型或外部搜索服务。"""
from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from app.parse import detect_document_type, parse_document
from app.pipeline import run_pipeline
from app.resume_gate import NotResumeError, ensure_resume
from app.search import CachedSearcher, SearchHit


class DocumentGateTests(unittest.TestCase):
    def test_pdf_bytes_override_the_filename(self):
        sample = Path(__file__).resolve().parent.parent / "samples" / "resume_lin.pdf"
        with tempfile.TemporaryDirectory() as directory:
            renamed = Path(directory) / "resume.txt"
            renamed.write_bytes(sample.read_bytes())
            self.assertEqual(detect_document_type(renamed), "pdf")
            parsed = parse_document(renamed)
            self.assertEqual(parsed.kind, "pdf")
            self.assertTrue(parsed.safe_text.strip())

    def test_docx_bytes_override_pdf_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            renamed = Path(directory) / "resume.pdf"
            with zipfile.ZipFile(renamed, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", "<document/>")
            self.assertEqual(detect_document_type(renamed), "docx")

    def test_fake_pdf_and_old_doc_stop_before_search(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "resume.pdf"
            fake.write_text("张三在北京大学获奖", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "无法从文件内容识别"):
                parse_document(fake)
            legacy = Path(directory) / "resume.doc"
            legacy.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32)
            with self.assertRaisesRegex(ValueError, "旧版 .doc"):
                parse_document(legacy)

    def test_empty_text_is_not_sent_to_claim_search(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "resume.md"
            empty.write_text("\n   \n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "没有可提取的简历正文"):
                parse_document(empty)

    def test_invalid_upload_never_calls_model_or_search(self):
        class Forbidden:
            def complete_json(self, *args, **kwargs):
                raise AssertionError("无效文件不得调用模型")

            def search(self, *args, **kwargs):
                raise AssertionError("无效文件不得发起搜索")

        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "resume.pdf"
            fake.write_bytes(b"not a PDF")
            with self.assertRaises(ValueError):
                run_pipeline(fake, llm=Forbidden(), searcher=Forbidden())

    def test_non_resume_content_stops_before_claim_search(self):
        class ForbiddenSearch:
            def search(self, *args, **kwargs):
                raise AssertionError("非简历不得搜索")

        class Classifier:
            def complete_json(self, system, user, **kwargs):
                self_called.append(system)
                return {"is_resume": False}

        self_called = []
        with tempfile.TemporaryDirectory() as directory:
            article = Path(directory) / "article.txt"
            article.write_text("张三\n2024年北京大学项目研究报告\n引言\n本文介绍实验方法。", encoding="utf-8")
            with self.assertRaises(NotResumeError):
                run_pipeline(article, llm=Classifier(), searcher=ForbiddenSearch())
        self.assertEqual(len(self_called), 1)

    def test_clear_resume_passes_without_extra_model_call(self):
        class Forbidden:
            def complete_json(self, *args, **kwargs):
                raise AssertionError("结构清楚的简历无需另调分类模型")

        sample = Path(__file__).resolve().parent.parent / "fixtures" / "resume_lin.md"
        parsed = parse_document(sample)
        ensure_resume(parsed, Forbidden())
        self.assertTrue(parsed.resume_validated)

    def test_resume_without_dates_is_not_rejected(self):
        class Forbidden:
            def complete_json(self, *args, **kwargs):
                raise AssertionError("不应要求简历一定写日期")

        with tempfile.TemporaryDirectory() as directory:
            resume = Path(directory) / "resume.md"
            resume.write_text("张三\n邮箱：zhang@example.com\n教育经历\n北京大学计算机专业\n项目经历\n参与搜索系统项目", encoding="utf-8")
            doc = parse_document(resume)
            ensure_resume(doc, Forbidden())
            self.assertTrue(doc.resume_validated)

    def test_uncertain_classification_does_not_search(self):
        class Uncertain:
            def complete_json(self, *args, **kwargs):
                return {"is_resume": "maybe"}

        with tempfile.TemporaryDirectory() as directory:
            article = Path(directory) / "person.txt"
            article.write_text("张三\n2024年在北京大学做项目研究。", encoding="utf-8")
            with self.assertRaises(NotResumeError):
                ensure_resume(parse_document(article), Uncertain())


class SearchCacheTests(unittest.TestCase):
    def test_same_query_and_domain_only_calls_provider_once(self):
        class Provider:
            def __init__(self):
                self.calls = []

            def search(self, query, site=None):
                self.calls.append((query, site))
                return [SearchHit("https://pku.edu.cn/notice")]

        provider = Provider()
        cached = CachedSearcher(provider)
        self.assertEqual(len(cached.search("张三 奖学金", site="pku.edu.cn")), 1)
        self.assertEqual(len(cached.search("张三 奖学金", site="pku.edu.cn")), 1)
        cached.search("张三 奖学金", site="tsinghua.edu.cn")
        self.assertEqual(provider.calls, [
            ("张三 奖学金", "pku.edu.cn"), ("张三 奖学金", "tsinghua.edu.cn")])


if __name__ == "__main__":
    unittest.main()
