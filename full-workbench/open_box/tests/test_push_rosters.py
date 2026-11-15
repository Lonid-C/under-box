"""跨学校推免名单回归，姓名均为测试数据。"""
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from app import judge
from app.collect import Budget, _official_roster_links, _read_and_extract, collect_for_claim, extract_evidence
from app.plan import Query
from app.schema import Claim
from app.search import PageFetcher, SearchHit, _office_text
from app.strategy import ResumeProfile, build_plan


def push_claim(receiver="中南大学", source="上海大学"):
    return Claim(id="push", raw_text=f"2026年保研至{receiver}", raw_locator="测试",
                 category="学历", date_label="2026", date_start="2026-09",
                 elements=[f"学校={receiver}", "入学方式=推免"],
                 entities={"org": receiver, "source_school_hint": source,
                           "source_field_hint": "音乐学院"})


class NoModel:
    def complete_json(self, *args, **kwargs):
        raise AssertionError("没有本人原文时不能调用模型")


def zipped(files):
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        for path, text in files.items():
            archive.writestr(path, text)
    return stream.getvalue()


class PushRosterTests(unittest.TestCase):
    def test_receiver_study_articles_are_kept_without_push_keywords(self):
        claim = push_claim(receiver="哈尔滨工业大学", source="哈尔滨工程大学")
        claim.elements = ["学校=哈尔滨工业大学", "入学方式=推免"]
        text = "认证主体：哈尔滨工业大学。张三，哈尔滨工业大学仪器学院2026级硕士研究生。"
        result = {"snippet": text, "publisher": "哈尔滨工业大学仪器学院",
                  "wechat_verified_subject": "哈尔滨工业大学",
                  "identity_signals": ["学校一致", "学院一致", "年级一致"],
                  "supports": ["学校=哈尔滨工业大学"]}
        llm = SimpleNamespace(complete_json=lambda *args, **kwargs: result)
        for url in ("https://mp.weixin.qq.com/s/student-profile",
                    "https://news.hit.edu.cn/student-profile.htm"):
            with self.subTest(url=url):
                hit = SearchHit(url, title="仪器青年说")
                found = _read_and_extract(claim, [hit], SimpleNamespace(get=lambda url: text),
                                          llm, set(), candidate_name="张三")
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0].source_tier, "B")
                assessed = judge.assess(claim, found)
                self.assertEqual(assessed.status, "part")
                self.assertEqual(assessed.proved, ["学校=哈尔滨工业大学"])
                self.assertEqual(assessed.unproved, ["入学方式=推免"])

    def test_explicit_push_admission_in_person_article_can_support_method(self):
        claim = push_claim(receiver="哈尔滨工业大学")
        text = "张三通过推荐免试进入哈尔滨工业大学仪器学院攻读硕士。"
        result = {"snippet": text, "publisher": "哈尔滨工业大学",
                  "identity_signals": ["学校一致", "学院一致"], "supports": claim.elements,
                  "wechat_verified_subject": "哈尔滨工业大学"}
        llm = SimpleNamespace(complete_json=lambda *args, **kwargs: result)
        for url in ("https://mp.weixin.qq.com/s/admission-story",
                    "https://news.hit.edu.cn/admission-story.htm"):
            with self.subTest(url=url):
                found = extract_evidence(claim, SearchHit(url, title="人物专访"), text,
                                         llm, candidate_name="张三")
                self.assertEqual(judge.assess(claim, [found]).status, "ok")

    def test_school_named_wechat_publisher_does_not_invent_certification(self):
        claim = push_claim(receiver="哈尔滨工业大学")
        text = "张三是哈尔滨工业大学硕士研究生。"
        result = {"snippet": text, "publisher": "哈尔滨工业大学留学交流群",
                  "identity_signals": ["学校一致"], "supports": ["学校=哈尔滨工业大学"]}
        llm = SimpleNamespace(complete_json=lambda *args, **kwargs: result)
        found = extract_evidence(claim, SearchHit("https://mp.weixin.qq.com/s/unverified"),
                                 text, llm, candidate_name="张三")
        self.assertIsNotNone(found)
        self.assertEqual(found.source_tier, "D")
        self.assertEqual(found.supports, ["学校=哈尔滨工业大学"])

    def test_bare_same_name_at_receiver_stays_identity_unconfirmed(self):
        claim = push_claim(receiver="哈尔滨工业大学")
        result = {"snippet": "活动嘉宾张三出席。", "publisher": "哈尔滨工业大学",
                  "supports": ["学校=哈尔滨工业大学"], "identity_signals": []}
        llm = SimpleNamespace(complete_json=lambda *args, **kwargs: result)
        found = extract_evidence(claim, SearchHit("https://news.hit.edu.cn/activity.htm", title="活动报道"),
                                 result["snippet"], llm, candidate_name="张三")
        self.assertEqual(judge.assess(claim, [found]).status, "who")

    def test_combined_notice_prioritizes_push_download_without_pdf_suffix(self):
        hit = SearchHit("https://yz.csu.edu.cn/info/1015/1410.htm",
                        title="关于公示2026年硕士研究生拟录取名单的通知")
        html = ('<main><a href="/system/_content/download.jsp?wbfileid=1">'
                '附件1：全国统考拟录取名单.pdf</a>'
                '<a href="/system/_content/download.jsp?wbfileid=2">'
                '附件2：拟录取推免生信息表.pdf</a></main>')
        links = _official_roster_links(html, hit, push_claim())
        self.assertEqual(len(links), 2)
        self.assertIn("wbfileid=2", links[0].url)

    def test_excel_and_vsb_extension_are_followed_but_foreign_hosts_are_not(self):
        hit = SearchHit("https://classics.scu.edu.cn/info/1164/1218.htm",
                        title="2026年预推免招生复试通知")
        html = ('<main><a href="/list.xlsx">复试名单.xlsx</a>'
                '<a href="/virtual_attach_file.vsb?e=.pdf&amp;id=2">推免名单</a>'
                '<a href="https://scu.edu.cn.evil.example/list.pdf">名单.pdf</a></main>')
        links = _official_roster_links(html, hit, push_claim(receiver="四川大学"))
        self.assertEqual({h.url for h in links}, {
            "https://classics.scu.edu.cn/list.xlsx",
            "https://classics.scu.edu.cn/virtual_attach_file.vsb?e=.pdf&id=2"})

    def test_index_follows_target_year_notice_then_document_in_shared_budget(self):
        index = SearchHit("https://yzb.nju.edu.cn/zxgg/listm.htm", title="最新公告")
        notice = "https://yzb.nju.edu.cn/notice.htm"
        document = "https://yzb.nju.edu.cn/roster.docx"
        content = {
            index.url: '<main><a href="/notice.htm">2026年接收推荐免试研究生拟录取名单公示</a>'
                       '<a href="/old.htm">2024年推免拟录取名单公示</a>'
                       '<a href="/policy.htm">2026年推免招生简章</a></main>',
            notice: '<main>名单见附件<a href="/roster.docx">附件：推免名单.docx</a></main>',
            document: '2026年推免拟录取名单\n姓名 | 学院 | 状态\n张三 | 音乐学院 | 拟录取',
        }
        budget = Budget(max_searches=0, max_page_reads=3)
        budget.note_read()
        with patch("app.collect.extract_evidence", return_value=None) as extract:
            _read_and_extract(push_claim(receiver="南京大学"), [index],
                              SimpleNamespace(get=lambda url: content[url]), NoModel(), set(),
                              candidate_name="张三", budget=budget)
        self.assertEqual(budget.page_reads, 3)
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(extract.call_args.args[1].url, document)

    def test_followed_documents_cannot_exceed_read_budget(self):
        hit = SearchHit("https://music.shu.edu.cn/notice.htm", title="2026年推免名单公示")
        calls = []

        def get(url):
            calls.append(url)
            if url == hit.url:
                return '<main><a href="list.pdf">推免名单.pdf</a></main>'
            raise AssertionError("附件不应在预算外请求")

        budget = Budget(max_searches=0, max_page_reads=1)
        budget.note_read()
        _read_and_extract(push_claim(), [hit], SimpleNamespace(get=get), NoModel(), set(),
                          candidate_name="张三", budget=budget)
        self.assertEqual(calls, [hit.url])
        self.assertEqual(budget.page_reads, 1)

    def test_empty_notice_body_follows_embedded_pdf_and_extracts_matching_name(self):
        hit = SearchHit("https://ugs.hrbeu.edu.cn/2022/0926/notice.htm",
                        title="获得免试攻读2023年硕士学位研究生资格学生名单")
        document = "https://ugs.hrbeu.edu.cn/_upload/article/files/list.pdf"
        claim = push_claim(receiver="哈尔滨工业大学", source="哈尔滨工程大学")
        claim.date_start = "2023-09"
        claim.date_label = "2023"
        content = {
            hit.url: '<div class="wp_articlecontent"><div class="wp_pdf_player" '
                     'pdfsrc="/_upload/article/files/list.pdf" '
                     'swsrc="/_upload/article/videos/list.swf"></div></div>',
            document: '获得免试攻读2023年硕士学位研究生资格学生名单\n序号 | 姓名\n905 | 张三',
        }
        calls = []

        def get(url):
            calls.append(url)
            return content[url]

        budget = Budget(max_searches=0, max_page_reads=2)
        budget.note_read()
        with patch("app.collect.extract_evidence", return_value=None) as extract:
            _read_and_extract(claim, [hit], SimpleNamespace(get=get), NoModel(), set(),
                              candidate_name="张三", budget=budget)
        self.assertEqual(calls, [hit.url, document])
        self.assertEqual(budget.page_reads, 2)
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(extract.call_args.args[1].url, document)
        self.assertIn("905 | 张三", extract.call_args.args[2])
        self.assertIn("2023年硕士学位研究生资格", extract.call_args.args[1].title)

    def test_embedded_pdf_keeps_official_domain_and_document_constraints(self):
        hit = SearchHit("https://ugs.hrbeu.edu.cn/notice.htm", title="2026年推免资格名单")
        html = ('<div pdfsrc="https://hrbeu.edu.cn.evil.example/roster.pdf"></div>'
                '<div pdfsrc="javascript:alert(1)"></div>'
                '<div pdfsrc="/roster.swf"></div>'
                '<a href="/roster.pdf">名单.pdf</a><div pdfsrc="/roster.pdf"></div>')
        links = _official_roster_links(html, hit,
                                       push_claim(source="哈尔滨工程大学"))
        self.assertEqual([link.url for link in links], ["https://ugs.hrbeu.edu.cn/roster.pdf"])

    def test_html_roster_preserves_backup_status(self):
        hit = SearchHit("https://music.shu.edu.cn/notice.htm", title="2026年推免名单公示")
        html = ('<main><table><tr><th>姓名</th><th>是否推荐</th></tr>'
                '<tr><td>张三</td><td>备选</td></tr></table></main>')
        with patch("app.collect.extract_evidence", return_value=None) as extract:
            _read_and_extract(push_claim(), [hit], SimpleNamespace(get=lambda url: html),
                              NoModel(), set(), candidate_name="张三")
        self.assertEqual(extract.call_count, 1)
        self.assertIn("张三 | 备选", extract.call_args.args[2])

    def test_unknown_source_school_is_discovered_and_cached_within_budget(self):
        claim = push_claim(source="测试理工大学")
        plan = build_plan(claim, "张三", ResumeProfile(identity="student"))
        self.assertTrue(any(q.site == "source_school" for q in plan.queries))
        calls = []

        def search(query, site=None):
            calls.append((query, site))
            if site is None:
                return [SearchHit("https://test-school.edu.cn/", title="测试理工大学官网")]
            return []

        cache = {}
        budget = Budget(max_searches=2, max_page_reads=0)
        collect_for_claim(claim, "张三", SimpleNamespace(search=search), NoModel(),
                          queries=[Query('"张三" 推免', site="source_school")],
                          budget=budget, school_domains=cache)
        self.assertEqual(calls, [('"测试理工大学" 官网', None),
                                 ('"张三" 推免', "test-school.edu.cn")])
        self.assertEqual(cache, {"测试理工大学": "test-school.edu.cn"})
        self.assertEqual(budget.searches, 2)
        page = SearchHit("https://college.test-school.edu.cn/notice.htm", title="2026年推免名单")
        links = _official_roster_links('<main><a href="list.pdf">名单.pdf</a></main>',
                                       page, claim, cache)
        self.assertEqual(len(links), 1)

    def test_empty_name_search_does_not_displace_roster_queries_with_variants(self):
        claim = push_claim()
        plan = build_plan(claim, "张三", ResumeProfile(identity="student"))
        calls = []

        def search(query, site=None):
            calls.append((query, site))
            return []

        collect_for_claim(claim, "张三", SimpleNamespace(search=search), NoModel(),
                          queries=plan.queries, budget=Budget(max_searches=8, max_page_reads=0))
        # 名单查询排在姓名查询前面：预算只有 8 次时，正式标题、公示年份、
        # 免试资格和接收方推免拟录取四条整批名单路径都必须已经发出。
        self.assertEqual(calls[0], ("免试攻读2026年硕士学位研究生 资格", "shu.edu.cn"))
        self.assertIn(("2025 推免 公示", "shu.edu.cn"), calls)
        self.assertIn(("2026 免试 资格 名单", "shu.edu.cn"), calls)
        self.assertIn(("2026 推免 拟录取 名单", "csu.edu.cn"), calls)
        # 单个引号词（"张三"）不再追加"去引号"的同义重查。
        self.assertNotIn(("张三", "csu.edu.cn"), calls)
        self.assertEqual(len(calls), 8)

    def test_verification_download_is_failure_not_roster_text(self):
        # content 与 text 保持一致：PageFetcher 现在自己按 meta/GBK 解码 content。
        response = SimpleNamespace(content="<html>请输入验证码下载附件</html>".encode(),
                                   text="<html>请输入验证码下载附件</html>", headers={},
                                   raise_for_status=lambda: None)
        url = "https://yz.csu.edu.cn/system/_content/download.jsp?id=2"
        fetcher = PageFetcher()
        with patch("app.search.robots_allows", return_value=True), \
             patch("httpx.get", return_value=response):
            self.assertIsNone(fetcher.get(url))
        self.assertIn("验证码", fetcher.failures[url])

    def test_xlsx_keeps_empty_columns_and_name_status_in_same_row(self):
        ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        data = zipped({
            "xl/workbook.xml": f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                               '<sheets><sheet name="推免复试名单" r:id="r1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="r1" Target="/xl/worksheets/sheet1.xml"/></Relationships>',
            "xl/sharedStrings.xml": f'<sst xmlns="{ns}"><si><t>张三</t></si><si><t>备选</t></si></sst>',
            "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{ns}"><sheetData><row>'
                                       '<c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>1</v></c>'
                                       '</row><row><c r="A2" t="inlineStr"><is><t>李四</t></is></c>'
                                       '<c r="C2" t="inlineStr"><is><t>拟录取</t></is></c>'
                                       '</row></sheetData></worksheet>',
        })
        self.assertEqual(_office_text(data), "工作表：推免复试名单\n张三 |  | 备选\n李四 |  | 拟录取")
        response = SimpleNamespace(content=data, headers={"content-type": "application/octet-stream"},
                                   text="binary", raise_for_status=lambda: None)
        with patch("app.search.robots_allows", return_value=True), \
             patch("httpx.get", return_value=response):
            self.assertIn("张三 |  | 备选", PageFetcher().get("https://school.example.edu/download?id=1"))

    def test_docx_keeps_table_rows_together(self):
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        data = zipped({"word/document.xml": f'<w:document xmlns:w="{ns}"><w:body>'
                      '<w:p><w:r><w:t>2026年推免资格名单</w:t></w:r></w:p>'
                      '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>张三</w:t></w:r></w:p></w:tc>'
                      '<w:tc><w:p><w:r><w:t>候补</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
                      '</w:body></w:document>'})
        self.assertEqual(_office_text(data), "2026年推免资格名单\n张三 | 候补")


if __name__ == "__main__":
    unittest.main()
