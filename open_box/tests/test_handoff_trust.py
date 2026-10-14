import unittest
from app.schema import Claim, Evidence, ElementProof, VerifiedClaim
from app.provenance import bind_page
from app.verification_agents import VerificationCoordinator


class HandoffTrustTests(unittest.TestCase):
    def test_legacy_proved_does_not_become_confirmed_anchor(self):
        c = Claim(id="p", category="论文", raw_text="合成人发表合成研究", raw_locator="合成",
                  date_label="", elements=["论文标题=合成研究"])
        ev = bind_page(Evidence(url="https://a.example/p", title="合成研究", publisher="合成出版方",
            source_tier="B", snippet=c.raw_text, supports=c.elements, identity_score=4,
            identity_signals=["学校一致", "学院一致"]), c.raw_text)
        coordinator = VerificationCoordinator()
        vc = VerifiedClaim(claim=c, status="part", proved=c.elements, evidence=[ev])
        coordinator.record([vc])
        self.assertEqual(coordinator.context_for(c).entities["verified_identity_anchors"], [])
        vc.element_proofs = [ElementProof(element=c.elements[0], state="verified",
                                          evidence_ids=[ev.evidence_id], accepted_tier="B")]
        coordinator.record([vc])
        self.assertEqual(len(coordinator.context_for(c).entities["verified_identity_anchors"]), 1)
        vc.element_proofs = []
        coordinator.record([vc])
        self.assertEqual(coordinator.context_for(c).entities["verified_identity_anchors"], [])
