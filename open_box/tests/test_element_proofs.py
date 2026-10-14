"""Synthetic proof-v2 contracts. Added before implementation; not yet executed."""
import unittest

from app import judge
from app.schema import Claim, Evidence, Report


def sample_claim():
    return Claim(id="synthetic", category="项目", raw_text="合成人在合成项目获一等奖并担任队长",
                 raw_locator="合成材料", date_label="2024",
                 elements=["奖项=一等奖", "角色=队长"])


def source(tier, element, url):
    from app.provenance import bind_page
    ev = Evidence(url=url, title="合成项目公告", publisher="合成机构", source_tier=tier,
                  snippet="合成人在合成项目获一等奖并担任队长", supports=[element],
                  identity_signals=["学校一致", "学院一致"])
    return bind_page(ev, ev.snippet)


class ElementProofTests(unittest.TestCase):
    def test_j01_a_d_cannot_stitch_into_ok(self):
        claim = sample_claim()
        vc = judge.assess(claim, [source("A", claim.elements[0], "https://a.example/award"),
                                  source("D", claim.elements[1], "https://d.example/role")])
        self.assertEqual(vc.status, "part")
        self.assertEqual(vc.proved, [claim.elements[0]])
        self.assertEqual([(p.state, p.accepted_tier) for p in vc.element_proofs],
                         [("verified", "A"), ("partial", "D")])

    def test_j02_duplicate_weak_signals_do_not_confirm_identity(self):
        self.assertEqual(judge.identity_score(["时间吻合"] * 10, []), 1)
        ev = source("A", "奖项=一等奖", "https://a.example/award")
        ev.identity_signals = ["时间吻合", "发布方为相关机构"] * 10
        judge.score_evidence(ev)
        self.assertFalse(judge._has_identity_anchor(ev))
        self.assertNotEqual(judge.assess(sample_claim(), [ev]).status, "ok")

    def test_each_element_may_have_its_own_ab_source(self):
        c = sample_claim()
        vc = judge.assess(c, [source("A", c.elements[0], "https://a.example/award"),
                              source("B", c.elements[1], "https://b.example/role")])
        self.assertEqual(vc.status, "ok")
        self.assertEqual(vc.proved, c.elements)

    def test_missing_provenance_cannot_bypass_new_verification(self):
        c = sample_claim()
        ev = source("A", c.elements[0], "https://a.example/award")
        ev.provenance_state = "legacy_unrecorded"
        self.assertNotEqual(judge.assess(c, [ev]).status, "ok")
        self.assertEqual(judge.assess(c, [ev]).proved, [])

    def test_old_report_retains_stored_verdict_without_rejudging(self):
        rep = Report.model_validate({"id": "old", "candidate_name": "合成人", "position": "",
            "report_no": "SYNTHETIC", "generated_at": "", "claims": [
                {"claim": sample_claim().model_dump(), "status": "ok"}]})
        self.assertEqual(rep.claims[0].status, "ok")
        self.assertEqual(rep.claims[0].element_proofs, [])
