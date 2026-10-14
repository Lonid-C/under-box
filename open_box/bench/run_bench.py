"""Offline synthetic replay; no network, model or user material."""
from __future__ import annotations
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.judge import assess
from app.schema import Claim, Evidence
from .metrics import summary


def run(path=None):
    root = Path(path or Path(__file__).parent / "fixtures" / "cases.json")
    cases = json.loads(root.read_text(encoding="utf-8"))
    rows = []
    for case in cases:
        claim = Claim(**case["claim"])
        from app.provenance import bind_page
        sources = []
        for row in case["evidence"]:
            ev = Evidence(**row)
            if "page_body" in row:
                ev = bind_page(ev, row["page_body"])
            sources.append(ev)
        vc = assess(claim, sources)
        rows.append({"case_id": case["case_id"], "gold": case["gold_element_proofs"],
                     "actual": [p.model_dump() for p in vc.element_proofs],
                     "gold_status": case["gold_status"], "actual_status": vc.status,
                     "tags": case.get("adversarial_tags", [])})
    return {"synthetic": True, "cases": rows, "metrics": summary(rows)}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
