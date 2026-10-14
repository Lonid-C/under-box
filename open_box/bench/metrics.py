def element_metrics(rows):
    gold = {(r["case_id"], p["element"]) for r in rows for p in r["gold"] if p["state"] == "verified"}
    got = {(r["case_id"], p["element"]) for r in rows for p in r["actual"] if p["state"] == "verified"}
    tp = len(gold & got)
    precision = tp / len(got) if got else 0
    recall = tp / len(gold) if gold else 0
    return {"elementPrecision": precision, "elementRecall": recall,
            "elementF1": 2 * precision * recall / (precision + recall) if precision + recall else 0,
            "sampleCount": len(rows)}


def summary(rows):
    unsafe_den = sum(r["gold_status"] != "ok" for r in rows)
    unsafe = sum(r["gold_status"] != "ok" and r["actual_status"] == "ok" for r in rows)
    return {**element_metrics(rows), "unsafeOkRate": unsafe / unsafe_den if unsafe_den else 0,
            "sameNameMisattributionRate": sum("same_name" in r.get("tags", ()) and r["actual_status"] in {"ok", "part"} for r in rows) / max(1, sum("same_name" in r.get("tags", ()) for r in rows))}
