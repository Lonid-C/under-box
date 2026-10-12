import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serve
from app.schema import Claim, Report
from app.collect import Budget
from app.internship import build_manual_review
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
