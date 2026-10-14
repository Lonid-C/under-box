from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class BenchCase:
    case_id: str
    claim: dict
    evidence: list[dict]
    gold_status: str
    gold_element_proofs: list[dict]
    synthetic: bool = True
    adversarial_tags: tuple[str, ...] = ()
