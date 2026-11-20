import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serve
from app.schema import Claim, Report, VerifiedClaim
from app.collect import Budget
from app.contact_fallback import attach_contact_reviews
from app.internship import build_manual_review, employment_company
from app.search import SearchHit
from types import SimpleNamespace


class ManualReviewContractTests(unittest.TestCase):
    def test_company_contact_is_exported_separately_from_empty_candidate_evidence(self):
        company="合成公司有限公司"
        url="https://www.qcc.com/firm/synthetic_contract.html"
        c=Claim(id="i",category="实习",raw_text=company+"实习",raw_locator="",date_label="",entities={"company":company},elements=["岗位=实习生"])
        html=f"<h1>{company}</h1><div>电话：010-12345678</div>"
        vc=build_manual_review(c,SimpleNamespace(search=lambda *a,**k:[SearchHit(url,company)]),SimpleNamespace(get=lambda u:html),Budget())
        report=Report(id="synthetic",candidate_name="合成人",position="",report_no="SYNTHETIC",generated_at="",claims=[vc])
        data=serve.to_presentation(report); row=data["claims"][0]
        self.assertEqual(row["evidence"],[]); self.assertEqual(row["proved"],[])
        self.assertEqual(row["manualReview"]["contacts"][0]["sourceUrl"],url)
        md=serve.report_to_markdown(data)
        self.assertIn("实习人工背调（尚未核实本人经历）",md)
        self.assertIn("010-12345678",md); self.assertIn(url,md)
        self.assertEqual(Report.model_validate_json(report.model_dump_json()).claims[0].manual_review,vc.manual_review)

    def test_old_history_reports_remain_loadable_without_new_fields(self):
        report={"id":"old","candidate_name":"合成人","position":"","report_no":"OLD","generated_at":"","claims":[{"claim":{"id":"i","category":"实习","raw_text":"实习","raw_locator":"","date_label":""},"status":"none"}]}
        vc=Report.model_validate(report).claims[0]
        self.assertIsNone(vc.manual_review); self.assertIsNone(vc.verification_agent)

    def test_employment_keeps_its_conclusion_and_reuses_company_lookup(self):
        company = "合成科技有限公司"
        url = "https://www.qcc.com/firm/synthetic_work.html"
        element = "岗位=工程师"
        claim = Claim(id="work", category="任职", raw_text=company + "工程师", raw_locator="",
                      date_label="", elements=["任职单位=" + company, element])
        confirmed = VerifiedClaim(claim=claim, status="ok", proved=[element])
        pending = VerifiedClaim(claim=claim.model_copy(update={"id": "work2"}), status="none")
        calls = []

        def search(query, site=None):
            calls.append((query, site))
            return [SearchHit(url, company)] if site == "qcc.com" else []

        html = f"<h1>{company}</h1><div>电话：010-12345678</div>"
        attach_contact_reviews([confirmed, pending], SimpleNamespace(search=search),
                               SimpleNamespace(get=lambda _: html), Budget)
        self.assertEqual(sum(site == "qcc.com" for _, site in calls), 1)
        self.assertIsNot(confirmed.manual_review, pending.manual_review)
        self.assertEqual(confirmed.status, "ok")
        self.assertEqual(confirmed.proved, [element])
        self.assertEqual(pending.status, "none")
        self.assertEqual(pending.proved, [])
        self.assertEqual(pending.evidence, [])
        self.assertEqual(pending.manual_review["kind"], "employment")
        self.assertEqual(pending.manual_review["contacts"][0]["sourceUrl"], url)

        report = Report(id="synthetic", candidate_name="合成人", position="", report_no="SYNTHETIC",
                        generated_at="", claims=[confirmed, pending])
        data = serve.to_presentation(report)
        self.assertIsNone(data["claims"][1]["verificationContacts"])
        md = serve.report_to_markdown(data)
        self.assertIn("任职经历 · 企业人工核查", md)
        self.assertIn(url, md)
        self.assertNotIn("实习人工背调", md)

    def test_company_lookup_rejects_non_grounded_name_and_keeps_academic_jobs_out(self):
        claim = Claim(id="work", category="任职", raw_text="在示例大学担任讲师", raw_locator="",
                      date_label="", entities={"company": "凭空生成有限公司", "org": "示例大学"})
        self.assertEqual(employment_company(claim), "")
        self.assertEqual(employment_company(claim.model_copy(update={"entities": {"company": "凭空生成有限公司"}})), "")
