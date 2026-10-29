"""Synthetic institutions only; no candidate data or paid APIs."""
import unittest
from unittest.mock import patch
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "underbox"))

from app.collect import Budget
from app.schema import Claim, VerifiedClaim, Report
from app.search import SearchHit
from app.internship import build_manual_review, COMPANY_FIELDS, registry_url
from app.education_review import build_education_review
from app.public_context import public_contacts, labeled_fields, visible_text, public_url
from app.qa_search import search_question
import serve


COMPANY = "测试软件有限公司"
FIRM = "https://www.qcc.com/firm/abc123.html"
TYC = "https://www.tianyancha.com/company/123456"
HTML = """<title>测试软件有限公司 - 企查查</title><h1>测试软件有限公司</h1>
<p>公司简介：测试软件有限公司是一家面向企业提供软件服务的公司。</p>
<table><tr><td>经营范围</td><td>软件开发；技术咨询</td></tr>
<tr><td>公司规模</td><td>100-499人</td></tr>
<tr><td>主营业务</td><td>企业管理软件</td></tr>
<tr><td>法定代表人</td><td>示例甲</td></tr></table>
<p>公司电话：010-12345678</p><p>联系邮箱：office@example.com</p>
<aside>相关企业 电话：010-99999999</aside>"""
SCHOOL = "测试大学"
PROGRAM = "https://cs.example.edu.cn/program"
CONTACT = "https://cs.example.edu.cn/contact"
PROGRAM_HTML = """<title>测试大学 计算机科学与技术专业介绍</title><h1>计算机科学与技术专业</h1>
<p>计算机科学与技术专业培养具备计算机基础理论和软件开发技能的学生，课程涵盖操作系统、数据库与网络。</p>
<a href="/contact">联系我们</a>"""
CONTACT_HTML = """<title>测试大学 计算机科学与技术 联系我们</title>
<h1>计算机科学与技术教务办公室联系方式</h1>
<p>联系人：示例老师</p><p>电话：010-12345678</p><p>邮箱：office@example.edu.cn</p>"""


def claim(category="实习"):
    return Claim(id="c01", raw_text=COMPANY + "实习" if category == "实习" else SCHOOL + "计算机科学与技术专业本科",
        raw_locator="第1页", category=category, date_label="2024",
        elements=["公司=" + COMPANY] if category == "实习" else ["学校=" + SCHOOL, "专业=计算机科学与技术"],
        entities={"company": COMPANY} if category == "实习" else {"school": SCHOOL})


class Searcher:
    def __init__(self, hits=()):
        self.hits, self.calls = list(hits), []
    def search(self, text, site=None):
        self.calls.append((text, site))
        return self.hits


class Fetcher:
    def __init__(self, pages=None, redirects=None):
        self.pages, self.resolved_urls, self.calls = pages or {}, redirects or {}, []
    def get(self, url):
        self.calls.append(url)
        return self.pages.get(url)


def report(vc):
    return Report(id="test", candidate_name="测试候选人", position="测试岗位", report_no="TEST",
                  generated_at="2026-10-29", claims=[vc])


class ContextTests(unittest.TestCase):
    def test_company_fields_contacts_and_rule_boundary(self):
        c = claim()
        search = Searcher([SearchHit(url=FIRM, title=COMPANY + " - 企查查"),
                           SearchHit(url=TYC, title=COMPANY + " - 天眼查")])
        vc = build_manual_review(c, search, Fetcher({FIRM: HTML, TYC: HTML}), Budget(3, 4, 45))
        self.assertEqual(vc.status, "none")
        self.assertEqual(vc.evidence, [])
        self.assertEqual(vc.proved, [])
        self.assertEqual(vc.manual_review["profile"]["size"]["value"], "100-499人")
        self.assertEqual(vc.manual_review["profile"]["legalRepresentative"]["value"], "示例甲")
        self.assertEqual(len(vc.manual_review["registryLinks"]), 2)
        self.assertTrue(vc.manual_review["tianyanchaQueryUrl"].startswith("https://www.tianyancha.com/search?key="))
        self.assertEqual(len(search.calls), 3)
        values = [c["value"] for c in vc.manual_review["contacts"]]
        self.assertIn("office@example.com", values)
        self.assertNotIn("010-99999999", values)

    def test_different_company_and_registry_redirect_do_not_supply_contacts(self):
        other = HTML.replace(COMPANY, "另一家有限公司")
        for fetcher in (Fetcher({FIRM: other}), Fetcher({FIRM: HTML}, {FIRM: "https://evil.example.com/"})):
            vc = build_manual_review(claim(), Searcher([SearchHit(FIRM, COMPANY + " - 企查查")]), fetcher, Budget(3, 4, 45))
            self.assertFalse(vc.manual_review["contacts"])
            self.assertFalse(vc.manual_review["companyUrl"])

    def test_brand_not_resolved_to_legal_entity(self):
        vc = build_manual_review(claim(), Searcher([SearchHit(FIRM, "测试软件（北京）有限公司 - 企查查")]),
                                 Fetcher({FIRM: HTML}), Budget(3, 4, 45))
        self.assertFalse(vc.manual_review["companyUrl"])
        self.assertFalse(vc.manual_review["profile"])

    def test_blocked_registry_snippet_has_explicit_provenance(self):
        vc = build_manual_review(claim(), Searcher([SearchHit(FIRM, COMPANY + " - 企查查",
            snippet=COMPANY + "成立于2020年。\n经营范围：软件开发\n公司规模：50-99人")]), Fetcher(), Budget(3, 4, 45))
        field = vc.manual_review["profile"]["size"]
        self.assertEqual(field["extractionMethod"], "search_snippet")
        self.assertFalse(vc.manual_review["contacts"])

    def test_missing_company_zero_budget_still_has_queries(self):
        search = Searcher()
        vc = build_manual_review(claim(), search, Fetcher(), Budget(0, 0, 45))
        self.assertFalse(search.calls)
        self.assertIn("qcc.com", vc.manual_review["queryUrl"])
        self.assertIn("tianyancha.com", vc.manual_review["tianyanchaQueryUrl"])

    def test_hidden_and_related_contact_excluded(self):
        text = visible_text('<p hidden>电话：010-99999999</p><script>电话：010-88888888</script><p>电话：010-12345678</p>')
        self.assertEqual([c["phone"] for c in public_contacts(text, FIRM, kind="办公")], ["010-12345678"])

    def test_masked_phone_and_fax_are_not_contacts(self):
        contacts = public_contacts("电话：010-1234****\n传真：010-99999999", FIRM, kind="办公")
        self.assertFalse(contacts)

    def test_size_is_not_inferred_from_registered_capital(self):
        fields = labeled_fields("注册资本：1000万元\n公司规模：\n主营业务：软件开发", COMPANY_FIELDS, FIRM)
        self.assertNotIn("size", fields)
        self.assertEqual(fields["mainBusiness"]["value"], "软件开发")

    def test_registry_hostname_and_paths_are_strict(self):
        for url in ("https://qcc.com.evil.com/firm/123.html", "https://www.tianyancha.com.evil.com/company/12",
                    "https://www.qcc.com/firm/a.html@evil.com", "javascript:alert(1)"):
            self.assertFalse(registry_url(url))

    def test_private_or_credential_url_rejected(self):
        for url in ("http://127.0.0.1", "http://192.168.0.1", "https://localhost.local", "https://u:p@example.com",
                    "https://example.com:8787"):
            self.assertFalse(public_url(url), url)

    def education(self, search, fetcher, budget=None):
        with patch("app.education_review._school_record", return_value={"name": SCHOOL, "domain": "example.edu.cn"}), \
             patch("app.education_review.school_domain", return_value="example.edu.cn"):
            return build_education_review(claim("学历"), search, fetcher, budget or Budget(3, 4, 45))

    def test_education_program_and_public_office(self):
        review = self.education(Searcher([SearchHit(PROGRAM, "计算机科学与技术专业介绍")]),
            Fetcher({PROGRAM: PROGRAM_HTML, CONTACT: CONTACT_HTML}))
        self.assertTrue(any(l["kind"] == "program" for l in review["links"]))
        self.assertEqual(review["profile"]["contactPerson"]["value"], "示例老师")
        self.assertIn("培养", review["profile"]["introduction"]["value"])
        self.assertTrue(any(c.get("email") == "office@example.edu.cn" for c in review["contacts"]))

    def test_education_different_department_contacts_excluded(self):
        wrong = CONTACT_HTML.replace("计算机科学与技术", "土木工程学院")
        review = self.education(Searcher([SearchHit(CONTACT, "土木工程学院联系方式")]), Fetcher({CONTACT: wrong}))
        self.assertFalse(review["contacts"])

    def test_education_external_redirect_excluded(self):
        review = self.education(Searcher([SearchHit(PROGRAM, "专业介绍")]),
            Fetcher({PROGRAM: PROGRAM_HTML}, {PROGRAM: "https://external.example.com/program"}))
        self.assertFalse(review["contacts"])
        self.assertFalse(review["profile"])
        self.assertTrue(all("example.edu.cn" in l["url"] for l in review["links"]))

    def test_university_general_office_is_labeled(self):
        html = '<title>测试大学教务处联系方式</title><p>电话：010-12345678</p>'
        review = self.education(Searcher([SearchHit(CONTACT, "教务处联系方式")]), Fetcher({CONTACT: html}))
        self.assertIn("未确认对应院系", review["contacts"][0]["kind"])

    def test_unknown_school_domain_not_guessed(self):
        with patch("app.education_review._school_record", return_value=None), \
             patch("app.education_review.school_domain", return_value=None):
            review = build_education_review(claim("学历"), Searcher(), Fetcher(), Budget(3, 4, 45))
        self.assertFalse(review["links"])
        self.assertIn("bing.com", review["queryUrl"])

    def test_education_zero_budget_has_catalog_home(self):
        review = self.education(Searcher(), Fetcher(), Budget(0, 0, 45))
        self.assertEqual(review["links"][0]["kind"], "school")
        self.assertFalse(review["contacts"])

    def test_qa_search_reports_sources_and_preserves_original(self):
        vc = VerifiedClaim(claim=claim(), status="none")
        rep = report(vc)
        before = rep.model_dump_json()
        search = Searcher([SearchHit(FIRM, COMPANY, snippet="企业资料摘要"), SearchHit("http://127.0.0.1/", "恶意地址")])
        web = search_question("c01 这家公司主营业务是什么？", rep, searcher=search, fetcher=Fetcher())
        self.assertIn(COMPANY, search.calls[0][0])
        self.assertNotIn(rep.candidate_name, search.calls[0][0])
        self.assertEqual(web["sources"][0]["id"], "W1")
        self.assertEqual(web["sources"][0]["extractionMethod"], "search_snippet")
        self.assertEqual(before, rep.model_dump_json())

    def test_failed_qa_search_is_explicit(self):
        class Broken:
            def search(self, *a, **k):
                raise RuntimeError("secret should not leak")
        web = search_question("查询企业官网", report(VerifiedClaim(claim=claim(), status="none")),
                              searcher=Broken(), fetcher=Fetcher())
        self.assertFalse(web["sources"])
        self.assertNotIn("secret", str(web))
        self.assertTrue(web["limitations"])

    def test_presentation_markdown_and_chat_accept_email_and_context(self):
        vc = build_manual_review(claim(), Searcher([SearchHit(FIRM, COMPANY)]), Fetcher({FIRM: HTML}), Budget(3,4,45))
        vc.education_review = self.education(Searcher([SearchHit(PROGRAM, "专业介绍")]),
            Fetcher({PROGRAM: PROGRAM_HTML, CONTACT: CONTACT_HTML}))
        rep = report(vc)
        ui = serve.to_presentation(rep)
        self.assertIn("educationReview", ui["claims"][0])
        self.assertIn("office@example.com", serve.render_material(rep))
        md = serve.report_to_markdown(ui)
        self.assertIn("公司规模", md)
        self.assertIn(CONTACT, md)
        self.assertIn("office@example.edu.cn", md)

    def test_prompt_distinguishes_supplemental_web_evidence(self):
        prompt = serve.build_prompt("报告", "问题", {"sources": [{"id": "W1", "url": FIRM}]})
        self.assertIn("W1", prompt)
        self.assertIn("外部不可信资料", prompt)
        self.assertIn("不证明本人经历", serve.QA_POLICY)



# HTTP-level coverage uses a temporary loopback server and synthetic report only.
class ContextAPITests(unittest.TestCase):
    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        self.rep = report(VerifiedClaim(claim=claim(), status="none"))
        self.state = {"oby": self.rep, "presentation": serve.to_presentation(self.rep), "profile": None}
        state = self.state
        class Handler(serve.Handler):
            def _deny_api(self, path):
                return False
            def _st(self):
                return state
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(self, payload):
        import http.client, json
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        connection.request("POST", "/api/ask/stream", json.dumps(payload),
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        body = response.read().decode()
        status = response.status
        connection.close()
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
        return status, events

    def test_default_chat_does_not_search(self):
        with patch("serve.stream_answer", return_value=iter([{"delta": "报告回答"}])), \
             patch("app.qa_search.search_question") as search:
            status, events = self.post({"question": "概括报告"})
        self.assertEqual(status, 200)
        search.assert_not_called()
        self.assertEqual(events[0]["delta"], "报告回答")
        self.assertTrue(events[-1]["done"])

    def test_web_sse_order_credentials_and_report_immutability(self):
        from app.api_config import SearchCredentials, search_credentials
        before = self.rep.model_dump_json()
        credentials = SearchCredentials("test-account-key")
        def web(question, rep):
            self.assertIs(search_credentials(), credentials)
            return {"sources": [{"id": "W1", "title": "企业官网", "url": FIRM,
                                  "accessedAt": "2026-10-29", "extractionMethod": "text"}],
                    "limitations": []}
        def answer(material, question, context):
            self.assertEqual(context["sources"][0]["id"], "W1")
            yield {"delta": "补充背景 [W1]"}
        with patch("serve.account_search_credentials", return_value=credentials), \
             patch("app.qa_search.search_question", side_effect=web), \
             patch("serve.stream_answer", side_effect=answer):
            status, events = self.post({"question": "企业主营业务", "webSearch": True})
        self.assertEqual(status, 200)
        self.assertEqual(events[0]["search"]["status"], "searching")
        self.assertEqual(events[1]["search"]["sources"][0]["id"], "W1")
        self.assertEqual(events[2]["delta"], "补充背景 [W1]")
        self.assertEqual(before, self.rep.model_dump_json())
        self.assertIsNone(search_credentials())

    def test_web_failure_is_visible_and_answer_still_streams(self):
        with patch("app.qa_search.search_question", side_effect=RuntimeError("private detail")), \
             patch("serve.stream_answer", return_value=iter([{"delta": "未取得资料"}])):
            status, events = self.post({"question": "企业主营业务", "webSearch": True})
        self.assertEqual(status, 200)
        self.assertFalse(events[1]["search"]["sources"])
        self.assertNotIn("private detail", str(events))
        self.assertTrue(events[-1]["done"])

    def test_invalid_json_shape_and_question_are_rejected(self):
        for value in ([], {"question": 123}, {"question": ""}, {"question": "x"*2001}):
            status, _ = self.post(value)
            self.assertEqual(status, 400)


class BackgroundIndexTests(unittest.TestCase):
    def test_background_index_is_scoped_and_preserves_verification_engine(self):
        from app.search import ZhipuSearcher
        from app.api_config import background_search, is_background_search
        searcher = ZhipuSearcher(api_key="test", engine="search_std")
        searcher.engine_open = "search_pro_sogou"
        with patch.object(searcher, "_call", return_value=[]) as call:
            searcher.search("问题")
            self.assertEqual(call.call_args.args[-1], "search_pro_sogou")
            with background_search():
                searcher.search("问题")
                self.assertEqual(call.call_args.args[-1], "search_std")
            self.assertFalse(is_background_search())
            searcher.search("问题")
            self.assertEqual(call.call_args.args[-1], "search_pro_sogou")

    def test_internship_plan_covers_both_registries_and_background(self):
        from app.plan import plan_with_notes
        plan = plan_with_notes(claim(), "测试候选人")
        self.assertEqual(plan.budget["searches"], 3)
        self.assertEqual(plan.budget["reads"], 4)
        self.assertEqual([q.site for q in plan.queries[:2]], ["qcc.com", "tianyancha.com"])
        self.assertTrue(all("测试候选人" not in q.text for q in plan.queries))

    def test_registry_suffixed_title_keeps_actual_source_url(self):
        url = TYC + "/sifa"
        vc = build_manual_review(claim(), Searcher([SearchHit(url, COMPANY + "法律诉讼",
            snippet=COMPANY + "。公司规模：50-99人")]), Fetcher(), Budget(3,4,45))
        self.assertEqual(vc.manual_review["registryLinks"][0]["url"], url)
        self.assertFalse(vc.manual_review["contacts"])



class SourceQualityTests(unittest.TestCase):
    def test_registration_metric_keeps_its_actual_label(self):
        fields = labeled_fields("参保人数：28人", COMPANY_FIELDS, FIRM)
        self.assertEqual(fields["size"]["label"], "参保人数")
        self.assertEqual(fields["size"]["value"], "28人")

    def test_shared_office_phone_fax_number_is_valid_but_fax_only_is_not(self):
        contacts = public_contacts("电话/传真：86-025-89683126", PROGRAM, kind="院系办公")
        self.assertEqual(contacts[0]["phone"], "86-025-89683126")
        self.assertFalse(public_contacts("传真：025-89683126", PROGRAM, kind="院系办公"))

    def test_login_redirect_is_a_snippet_not_a_read_source(self):
        vc = VerifiedClaim(claim=claim(), status="none")
        fetcher = Fetcher({FIRM: "<title>用户登录</title><p>请先登录</p>"},
                          {FIRM: "https://www.qcc.com/user-login"})
        result = search_question("公司介绍", report(vc), searcher=Searcher([
            SearchHit(FIRM, COMPANY, snippet=COMPANY + "的资料摘要")]), fetcher=fetcher)
        self.assertEqual(result["sources"][0]["extractionMethod"], "search_snippet")
        self.assertEqual(result["sources"][0]["url"], FIRM)


if __name__ == "__main__":
    unittest.main()
