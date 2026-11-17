import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import store


class ReviewWorkflowTests(unittest.TestCase):
    def test_review_does_not_overwrite_automatic_verdict_and_rejects_other_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = store.init(Path(tmp) / "synthetic.db")
            with store.connect(db) as conn:
                store.create_user(conn, "reviewer", "合成复核员", "synthetic-password", role="admin")
                store.create_user(conn, "other", "合成用户", "synthetic-password")
                rid = store.save_report(conn, "reviewer", {"claims": [{"id": "c1", "status": "none"}]})
                kw = dict(owner_id="reviewer", actor_id="reviewer", report_id=rid, claim_id="c1",
                          source="synthetic-material-index", reason="合成补证，待人工核对")
                self.assertEqual(store.append_review(conn, **kw, action="open"), 1)
                self.assertEqual(store.append_review(conn, **kw, action="supplement", expected_version=1), 2)
                self.assertEqual(store.append_review(conn, **kw, action="decide", expected_version=2,
                                                    outcome="unresolved"), 3)
                rep = store.get_report(conn, "reviewer", rid)
                self.assertEqual(rep["claims"][0]["status"], "none")
                self.assertEqual(len(rep["reviewHistory"]), 3)
                with self.assertRaises(PermissionError):
                    store.append_review(conn, **{**kw, "actor_id": "other"}, action="decide")
                self.assertIsNone(store.get_report(conn, "other", rid))
                store.clear_history(conn, "reviewer")
                self.assertIsNone(store.get_report(conn, "reviewer", rid))
