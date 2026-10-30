"""Public contact routes and evidence boundaries; synthetic claims, no network."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "underbox"))
from app.schema import Claim, VerifiedClaim, Report
from app.collect import Budget
from app.search import SearchHit
from app.contact_fallback import (build_contact_review, attach_contact_reviews, directory_contacts,
    needs_contacts, valid_contact, discover_target, DIRECTORY)
import serve


def vc(category="学历", raw="北师香港浸会大学 本科", status="none", **kwargs):
    return VerifiedClaim(claim=Claim(id="c01", raw_text=raw, raw_locator="第1页", category=category,
        date_label="2026", entities=kwargs.pop("entities", {"school":"北师香港浸会大学"})),
        status=status, unproved=["待核对事项"], **kwargs)


class Searcher:
    def __init__(self, hits=()): self.hits, self.calls = list(hits), []
    def search(self, text, site=None): self.calls.append((text,site)); return self.hits


class Fetcher:
    def __init__(self, pages=None, redirects=None): self.pages, self.resolved_urls, self.calls=pages or {}, redirects or {}, []
    def get(self,url): self.calls.append(url); return self.pages.get(url)


class ContactTests(unittest.TestCase):
    def test_statuses_and_project_exclusion(self):
        for status in ("part","ask","none","who"):
            self.assertTrue(needs_contacts(vc(status=status)))
        self.assertIsNone(build_contact_review(vc(status="ok")))
        for category in ("项目","开源项目"):
            self.assertIsNone(build_contact_review(vc(category, raw="IMC Prosperity")))

    def test_directory_routes_do_not_cross_schools(self):
        self.assertTrue(build_contact_review(vc())["contacts"])
        other=build_contact_review(vc(raw="另一所大学本科", entities={"school":"另一所大学"}))
        self.assertFalse(other["contacts"])
        self.assertEqual(other["state"],"not_found")

    def test_mcm_does_not_match_cumcm(self):
        for raw in ("CUMCM 广东省二等奖", "全国大学生数学建模竞赛"):
            contacts=directory_contacts(vc("竞赛",raw).claim)
            self.assertNotIn("mcm@comap.com", [c["value"] for c in contacts])
        contacts=directory_contacts(vc("竞赛","2026 MCM 美赛").claim)
        self.assertIn("mcm@comap.com", [c["value"] for c in contacts])

    def test_exam_routes_are_based_on_actual_exam(self):
        r=build_contact_review(vc("校内荣誉","英语 CET-4、CET-6，雅思 6.5"))
        self.assertEqual({c["value"] for c in r["contacts"]},{"021-52583311","010-82345671"})
        self.assertTrue(all("成绩核查" in c["scope"] or "考后服务" in c["scope"] for c in r["contacts"]))

    def test_hkbu_employer_route_does_not_use_bnbu_substring(self):
        claim = vc("实习","北京师范大学-香港浸会大学联合国际学院助理", manual_review={"company":"北京师范大学-香港浸会大学联合国际学院"})
        self.assertNotIn("hro@hkbu.edu.hk",[c["value"] for c in build_contact_review(claim)["contacts"]])
        claim.manual_review={"company":"香港浸会大学"}
        self.assertNotIn("hro@hkbu.edu.hk",[c["value"] for c in build_contact_review(claim)["contacts"]])
        claim.claim.raw_text="科研助理｜香港浸会大学研究院-资讯系统系"
        self.assertIn("hro@hkbu.edu.hk",[c["value"] for c in build_contact_review(claim)["contacts"]])

    def test_contest_and_conference_routes_work_without_school(self):
        for category,raw,value in [("竞赛","IMC Prosperity 4","prosperity@imc.com"),
            ("竞赛","2025 MathorCup 大数据竞赛","bigdata@mathorcup.org"),
            ("会议","2026 INFORMS ISMS Marketing Science Conference","informs@informs.org")]:
            r=build_contact_review(vc(category,raw,entities={}))
            self.assertIn(value,[c["value"] for c in r["contacts"]])
            self.assertTrue(all(c.get("sourceUrl") and c.get("scope") for c in r["contacts"]))

    def test_related_company_contact_keeps_subject_note(self):
        contact={"type":"phone","value":"0755-12345678","excerpt":"电话：0755-12345678", "sourceUrl":"https://example.com/contact", "kind":"2024招聘公告公开电话"}
        review={"company":"测试中心","contacts":[],"relatedCompanies":[{"company":"测试中心深圳公司", "contacts":[contact], "subjectNote":"关联公司，需确认实际用工主体"}]}
        r=build_contact_review(vc("实习","测试中心实习",manual_review=review))
        self.assertEqual(r["contacts"][0]["institution"],"测试中心深圳公司")
        self.assertEqual(r["contacts"][0]["subjectNote"],"关联公司，需确认实际用工主体")

    def test_unread_snippet_private_and_candidate_contact_not_used(self):
        contact={"type":"phone","value":"0755-12345678", "sourceUrl":"http://127.0.0.1/contact", "excerpt":"电话0755-12345678"}
        r=build_contact_review(vc("任职","测试公司任职，个人手机13800138000",entities={"phone":"13800138000"},manual_review={"company":"测试公司","contacts":[contact]}))
        self.assertFalse(r["contacts"])
        self.assertFalse(valid_contact({**contact,"sourceUrl":"https://example.com","excerpt":"其他文字"}))

    def test_curated_phone_normalization_has_source(self):
        data=json.loads(DIRECTORY.read_text())
        for route in data["routes"]:
            for item in route["contacts"]:
                self.assertTrue(item["value"])
                self.assertTrue(route["sourceUrl"].startswith("https://"))
                self.assertTrue(route["excerpt"])
                self.assertEqual(route["verifiedAt"],"2026-10-30")

    def test_official_lookup_reads_contact_page_and_reuses_same_target(self):
        home="https://example.edu.cn/"
        url=home+"contact"
        search=Searcher([SearchHit(url,"测试大学 教务处 联系我们")])
        fetch=Fetcher({url:"<title>测试大学 教务处 联系我们</title><p>电话：010-12345678</p><p>邮箱：ar@example.edu.cn</p>"})
        a=vc(raw="测试大学本科",entities={"school":"测试大学"})
        b=vc(raw="测试大学本科",entities={"school":"测试大学"});b.claim.id="c02"
        with patch("app.contact_fallback.school_domain",return_value="example.edu.cn"):
            attach_contact_reviews([a,b],search,fetch,lambda:Budget(1,2,10))
        self.assertTrue(a.verification_contacts["contacts"])
        self.assertEqual(a.verification_contacts,b.verification_contacts)
        self.assertEqual(len(search.calls),1)
        self.assertEqual(len(fetch.calls),1)

    def test_external_redirect_cannot_supply_official_contacts(self):
        url="https://example.edu.cn/contact"
        search=Searcher([SearchHit(url,"测试大学 联系我们")])
        fetch=Fetcher({url:"<title>联系我们</title><p>电话010-12345678</p>"},{url:"https://other.example.com/contact"})
        with patch("app.contact_fallback.school_domain",return_value="example.edu.cn"):
            result=build_contact_review(vc(raw="测试大学本科",entities={"school":"测试大学"}),search,fetch,Budget(1,2,10))
        self.assertFalse(result["contacts"])

    def test_unknown_issuer_requires_actual_home_owner(self):
        claim=vc("任职","示例技术有限公司工程师",entities={"employer":"示例技术有限公司"}).claim
        search=Searcher([SearchHit("https://example.com/profile","示例技术有限公司")])
        for html in ("<title>示例技术有限公司</title><p>目录网站 版权所有</p>","<title>目录网站</title><p>示例技术有限公司</p>"):
            self.assertIsNone(discover_target(claim,search,Fetcher({"https://example.com/":html}),Budget(1,2,10),[]))
        html="<title>示例技术有限公司</title><footer>©2026 示例技术有限公司 版权所有</footer>"
        self.assertEqual(discover_target(claim,search,Fetcher({"https://example.com/":html}),Budget(1,2,10),[])[1],"example.com")

    def test_zero_budget_is_explicit_and_does_not_request(self):
        search=Searcher();fetch=Fetcher()
        result=build_contact_review(vc("任职","示例公司任职",entities={"org":"示例公司"}),search,fetch,Budget(0,0,10))
        self.assertFalse(search.calls);self.assertFalse(fetch.calls)
        self.assertEqual(result["state"],"not_found")
        self.assertTrue(result["limitations"])

    def test_contact_is_not_evidence_and_reaches_export_and_qa(self):
        c=vc("竞赛","IMC Prosperity 4",status="part",proved=["参赛"])
        before=c.model_dump(exclude={"verification_contacts"})
        attach_contact_reviews([c],Searcher(),Fetcher(),lambda:Budget(0,0,10))
        self.assertEqual(before,c.model_dump(exclude={"verification_contacts"}))
        rep=Report(id="test",candidate_name="示例",position="测试",report_no="T",generated_at="2026-10-30",claims=[c])
        presentation=serve.to_presentation(rep)
        self.assertIn("verificationContacts",presentation["claims"][0])
        c.verification_contacts=None
        self.assertIn("prosperity@imc.com", serve.render_material(rep))
        self.assertTrue(serve.to_presentation(rep)["claims"][0]["verificationContacts"]["contacts"])
        for text in (serve.render_material(rep),serve.report_to_markdown(presentation)):
            self.assertIn("prosperity@imc.com",text)
            self.assertIn("咨询范围",text)
            self.assertIn("https://prosperity.imc.com/",text)


if __name__ == "__main__": unittest.main()
