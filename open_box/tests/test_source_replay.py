"""Synthetic source replay: exact matching, boundaries and SSE compatibility."""
import hashlib
import json
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "underbox"))
from app.source_replay import load_snapshot, report_from_snapshot
from app.parse import ParsedDocument
from app.schema import Report, Claim, VerifiedClaim
import serve


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = b"synthetic resume bytes"
        self.sha = hashlib.sha256(self.data).hexdigest()
        report = Report(id="snapshot", candidate_name="示例甲", position="测试岗位", report_no="TEST",
            generated_at="2026-10-29", claims=[VerifiedClaim(claim=Claim(id="c01",raw_text="示例大学项目",
            category="项目",raw_locator="第1页",date_label="2026",elements=["本人参与"]),status="none"),
            VerifiedClaim(claim=Claim(id="c02",raw_text="示例竞赛",category="竞赛",raw_locator="第1页",
            date_label="2026",elements=["奖项"]),status="ask")])
        self.raw = {"version":1,"inputSha256":self.sha,"cachedAt":"2026-10-29T00:00:00Z",
                    "profile":{"identity":"student","level":"intern","name":"示例甲"},"report":report.model_dump()}
        self.path = Path(self.tmp.name)/(self.sha+".json")
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.raw,ensure_ascii=False))

    def snapshot(self):
        return load_snapshot(self.data,self.tmp.name)

    def test_exact_bytes_only_renaming_irrelevant(self):
        self.assertIsNotNone(self.snapshot())
        self.assertIsNone(load_snapshot(self.data+b"changed",self.tmp.name))
        self.assertIsNone(load_snapshot(b"",self.tmp.name))

    def test_malformed_and_wrong_fingerprint_fall_through(self):
        self.raw["inputSha256"]="0"*64;self.write();self.assertIsNone(self.snapshot())
        self.path.write_text("broken");self.assertIsNone(self.snapshot())

    def test_subset_order_skipped_and_deep_copy(self):
        snap=self.snapshot()
        report=report_from_snapshot(snap,filename="renamed.pdf",claim_ids=["c02"])
        self.assertEqual([v.claim.id for v in report.claims],["c02"])
        self.assertEqual([v.id for v in report.skipped_claims],["c01"])
        self.assertEqual(report.claims[0].status,"ask")
        self.assertEqual(report.source_cache["kind"],"source_replay")
        self.assertEqual(report.mode,"mock")
        self.assertFalse(report.fictional)
        self.assertEqual(len(snap["report"].claims),2)
        self.assertNotEqual(report.report_no,report_from_snapshot(snap,filename="x").report_no)

    def test_empty_or_unknown_selection_rejected(self):
        for ids in ([],["missing"],["c01","missing"]):
            with self.assertRaises(ValueError): report_from_snapshot(self.snapshot(),filename="x",claim_ids=ids)

    def test_split_selected_full_upload_and_export_provenance(self):
        snap=self.snapshot();q=queue.Queue();holder={}
        doc=ParsedDocument("synthetic",[],"input","pdf")
        with patch("app.parse.parse_document",return_value=doc), patch("app.source_replay.load_snapshot",return_value=snap), patch("serve.time.sleep"), patch("app.llm.build_llm",side_effect=AssertionError("no network model calls")):
            serve.run_split_job(self.data,"renamed.pdf",q,holder)
            events=list(q.queue);done=next(e for e in events if e and e.get("done"))
            self.assertFalse(done.get("failed"));self.assertEqual(len(done["split"]["claims"]),2)
            self.assertEqual(done["split"]["sourceCache"]["kind"],"source_replay")
            token=done["split"]["token"]
            q=queue.Queue();serve.run_verify_selected_job(token,["c01"],q,holder)
            done=next(e for e in list(q.queue) if e and e.get("done"))
            self.assertFalse(done.get("failed"));self.assertEqual(len(done["report"]["claims"]),1)
            self.assertEqual(done["report"]["claims"][0]["status"],"none")
            self.assertIn("公开来源缓存",serve.report_to_markdown(done["report"]))
            self.assertIn("演示",serve.render_material(holder["oby"]))
            q=queue.Queue();serve.run_verify_job(self.data,"x.pdf",q,holder)
            done=next(e for e in list(q.queue) if e and e.get("done"))
            self.assertEqual(len(done["report"]["claims"]),2)
            with serve._PREPARED_LOCK: serve.PREPARED.pop(token,None)

    def test_nonmatch_uses_regular_pipeline(self):
        q=queue.Queue()
        with patch("app.parse.parse_document",return_value=ParsedDocument("synthetic",[],"input","pdf")), patch("app.source_replay.load_snapshot",return_value=None), patch("app.llm.build_llm",side_effect=ValueError("ordinary pipeline reached")):
            serve.run_split_job(b"other","same-name.pdf",q,{})
        done=next(e for e in list(q.queue) if e and e.get("done"))
        self.assertTrue(done["failed"])
        self.assertIn("ordinary pipeline reached",done["error"])

    def test_http_selection_route_handles_replay_entries(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        server=ThreadingHTTPServer(("127.0.0.1",0),serve.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        token=serve._remember_prepared({"snapshot":self.snapshot(),"filename":"x.pdf","fileType":"pdf"})
        self.addCleanup(serve.PREPARED.pop,token,None)
        with patch("serve.time.sleep"), patch("serve.Handler._authed",return_value=True):
            conn=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=10)
            conn.request("POST","/api/verify/stream",json.dumps({"token":token,"claimIds":["c02"]}),{"Content-Type":"application/json"})
            response=conn.getresponse();body=response.read().decode();conn.close()
            self.assertEqual(response.status,200)
            events=[json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
            done=next(e for e in events if e.get("done"))
            self.assertEqual(done["report"]["claims"][0]["id"],"c02")
            self.assertEqual(done["report"]["sourceCache"]["kind"],"source_replay")
            self.assertTrue(any(e.get("sourceCache") for e in events))
            for payload in ({"token":token,"claimIds":["missing"]},{"token":token,"claimIds":["c02"],"githubMaterials":{"c02":"x"}}):
                conn=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=10)
                conn.request("POST","/api/verify/stream",json.dumps(payload),{"Content-Type":"application/json"})
                res=conn.getresponse();res.read();conn.close();self.assertEqual(res.status,400)

if __name__ == "__main__": unittest.main()
