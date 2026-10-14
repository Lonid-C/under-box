"""Deterministic retrieval coverage, never a candidate score."""
from __future__ import annotations

RETRIEVAL_STATES = {"complete", "partial", "provider_error", "blocked", "timeout",
                    "budget_exhausted", "not_attempted", "canceled"}


def summarize(*, selected_elements, proofs=(), queries_planned=0, queries_attempted=0,
              query_successes=0, pages_attempted=0, pages_read_success=0,
              page_failures=None, verified_evidence_count=0, unverified_lead_count=0,
              retrieval_status="complete", limitations=None, skipped=False):
    selected = list(dict.fromkeys(selected_elements or []))
    states = {p.element: p.state for p in proofs}
    covered = [e for e in selected if states.get(e) == "verified"]
    if retrieval_status not in RETRIEVAL_STATES:
        raise ValueError("invalid retrieval_status")
    return {"retrievalStatus": retrieval_status, "queriesPlanned": queries_planned,
            "queriesAttempted": queries_attempted, "querySuccesses": query_successes,
            "pagesAttempted": pages_attempted, "pagesReadSuccess": pages_read_success,
            "pageFailures": page_failures or [], "verifiedEvidenceCount": verified_evidence_count,
            "unverifiedLeadCount": unverified_lead_count, "coveredElements": covered,
            "selectedElements": selected, "coverageRatio": len(covered) / len(selected) if selected else None,
            "limitations": limitations or [], "skipped": bool(skipped),
            "neutralNoRecord": retrieval_status == "complete" and verified_evidence_count == 0
                               and unverified_lead_count == 0}


def from_claim(vc, **stats):
    ids = {eid for p in vc.element_proofs if p.state == "verified" for eid in p.evidence_ids}
    return summarize(selected_elements=vc.claim.elements, proofs=vc.element_proofs,
                     verified_evidence_count=len(ids), **stats)


def report_coverage(claims, skipped=()):
    eligible = [vc for vc in claims if vc.claim.category != "实习"]
    selected = sum(len(set(vc.claim.elements)) for vc in eligible)
    covered = sum(sum(p.state == "verified" for p in vc.element_proofs) for vc in eligible)
    return {"selectedElements": selected, "coveredElements": covered,
            "coverageRatio": covered / selected if selected else None,
            "manualClaims": [vc.claim.id for vc in claims if vc.claim.category == "实习"],
            "skippedClaims": [c.id for c in skipped],
            "note": "仅统计本次选择自动核验的要素；实习人工背调及跳过项不在分母内，不评价候选人。"}
