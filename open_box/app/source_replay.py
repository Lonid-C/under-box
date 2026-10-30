"""Private exact-file source snapshots for labelled presentation replay.

No resume, candidate name or snapshot is distributed with the repository.
Missing/malformed snapshots fall through to the regular verification pipeline.
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .schema import Report
from .strategy import ResumeProfile


def load_snapshot(data: bytes, directory=None):
    digest = hashlib.sha256(data).hexdigest()
    folder = Path(directory or os.environ.get("UNDERBOX_SOURCE_REPLAY_DIR") or
                  Path(__file__).resolve().parents[2] / "private" / "source-replay")
    path = folder / (digest + ".json")
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw["version"] != 1 or raw["inputSha256"] != digest:
            return None
        report = Report.model_validate(raw["report"])
        if not report.claims or len({v.claim.id for v in report.claims}) != len(report.claims):
            return None
        datetime.fromisoformat(raw["cachedAt"].replace("Z", "+00:00"))
        profile = ResumeProfile(**raw.get("profile", {}))
        return {"report": report, "profile": profile, "cachedAt": raw["cachedAt"]}
    except (ValueError, KeyError, TypeError, OSError):
        logging.getLogger(__name__).warning("Ignoring invalid private source snapshot")
        return None


def metadata(snapshot):
    return {"kind": "source_replay", "label": "公开来源缓存",
            "cachedAt": snapshot["cachedAt"],
            "note": "依据已整理的公开资料生成，来源整理时间保留供查阅。"}


def report_from_snapshot(snapshot, *, filename, claim_ids=None, risks=()):
    report = snapshot["report"].model_copy(deep=True)
    selected = None if claim_ids is None else set(claim_ids)
    if selected is not None:
        valid = {v.claim.id for v in report.claims}
        if not selected or not selected <= valid:
            raise ValueError("请选择有效的核验条目")
        report.skipped_claims = [v.claim for v in report.claims if v.claim.id not in selected]
        report.claims = [v for v in report.claims if v.claim.id in selected]
    report.id = "replay-" + uuid.uuid4().hex
    report.report_no = "UB-" + uuid.uuid4().hex[:10].upper()
    report.generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report.source_file = filename
    report.mode = "mock"
    report.fictional = False
    report.input_risks = list(risks)
    report.source_cache = metadata(snapshot)
    return report
