"""Collaboration records follow the same account/history isolation as reports."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import serve
import store
from app.schema import Claim, VerifiedClaim, Report


def report():
    c=Claim(id="w",category="任职",raw_text="合成人在合成公司任工程师",raw_locator="测试",date_label="",elements=["岗位=工程师"])
    workflow={"version":2,"tasks":[{"claimId":"w","agent":{"id":"employment","name":"工作经历核验"},
               "sourceAccess":{"events":[{"sourceName":"独立第三方公开网页","state":"empty"}]},"handoffs":[]}],
              "handoffs":[{"fromClaim":"p","toClaim":"w","element":"论文标题=合成研究",
                           "urls":["https://publisher.example/paper"],"use":"navigation_only"}],
              "note":"线索不构成岗位证据。"}
    vc=VerifiedClaim(claim=c,status="none",proved=[],unproved=c.elements,
                     verification_agent={"id":"employment","name":"工作经历核验"})
    return Report(id="test",candidate_name="合成人",position="",report_no="SYNTHETIC",generated_at="",
                  claims=[vc],verification_workflow=workflow)


class WorkflowContractTests(unittest.TestCase):
    def test_collaboration_and_actual_sources_are_in_json_and_markdown_without_added_proof(self):
        rep=report();data=serve.to_presentation(rep)
        self.assertEqual(data["verificationWorkflow"],rep.verification_workflow)
        self.assertEqual(data["claims"][0]["proved"],[])
        self.assertEqual(data["claims"][0]["evidence"],[])
        md=serve.report_to_markdown(data)
        for text in ("核验分工与协作","工作经历核验","独立第三方公开网页","p → w","合成研究","线索不构成岗位证据"):
            self.assertIn(text,md)
        self.assertEqual(Report.model_validate_json(rep.model_dump_json()).verification_workflow,rep.verification_workflow)

    def test_workflow_persists_with_latest_ten_and_cannot_be_read_by_another_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/"users.db";store.init(db)
            with store.connect(db) as conn:
                for uid in ("synthetic-a","synthetic-b"):store.create_user(conn,uid,uid,"synthetic-password")
                ids=[]
                for _ in range(12):ids.append(store.save_report(conn,"synthetic-a",serve.to_presentation(report())))
                conn.commit()
                self.assertEqual(len(store.list_reports(conn,"synthetic-a")),10)
                self.assertIsNone(store.get_report(conn,"synthetic-a",ids[0]))
                self.assertIsNone(store.get_report(conn,"synthetic-b",ids[-1]))
                self.assertEqual(store.get_report(conn,"synthetic-a",ids[-1])["verificationWorkflow"]["version"],2)
            with store.connect(db) as conn:
                self.assertEqual(store.get_report(conn,"synthetic-a",ids[-1])["verificationWorkflow"]["tasks"][0]["claimId"],"w")

    def test_old_reports_without_workflow_keep_their_contract(self):
        data=report().model_dump();data.pop("verification_workflow")
        loaded=Report.model_validate(data)
        self.assertIsNone(loaded.verification_workflow)
        self.assertIsNone(serve.to_presentation(loaded)["verificationWorkflow"])
