"""履历核验的**规则层**机器人：一次调用一条命令，JSON 进 JSON 出。

这一层刻意只做纯规则的事——解析与风险检测、检索计划、来源分级、身份与状态判定。
凡是需要"理解"的（拆陈述、摘录证据、写澄清问题）都不在这里，由 harness 的模型承担。
这样 open_box 原有的 23 项验收在 harness 里依然成立，判定结果可复现。

调用方（dsh 插件）通过 PYTHONPATH 指向 open_box 根目录来导入它的 app.* 模块，
因此 open_box 本身不需要任何改动。

stdin : {"cmd": "...", ...}
stdout: {"ok": true, "data": ...} | {"ok": false, "error": "..."}
"""
from __future__ import annotations

import json
import sys
from typing import Any

# ── 对模型友好的错误 ──────────────────────────────────────────────────────


class BridgeError(Exception):
    """可预期的输入错误，直接回给模型，不打印堆栈。"""


def _require(payload: dict, key: str) -> Any:
    if key not in payload:
        raise BridgeError(f"缺少参数 {key!r}")
    return payload[key]


CATEGORIES = [
    "学历", "校内荣誉", "奖学金", "学生工作", "竞赛",
    "论文", "专利", "开源项目", "实习", "任职",
]


def _load():
    """延迟导入，让 --help 之类的调用不必装 pydantic。"""
    from app import judge
    from app.questions import default_next_step
    from app.schema import Claim, Evidence

    return judge, default_next_step, Claim, Evidence


# ── 命令实现 ─────────────────────────────────────────────────────────────


def cmd_ingest(payload: dict) -> dict:
    """解析简历（PDF / DOCX / MD / TXT）并做输入风险检测。

    返回的 safe_text 才是可以送进模型的文本；命中的行已被剔除并留占位。
    """
    from app.parse import parse_document

    path = _require(payload, "path")
    doc = parse_document(path)
    return {
        "source": doc.source,
        "safe_text": doc.safe_text,
        "raw_chars": len(doc.raw_text),
        "safe_chars": len(doc.safe_text),
        "risks": [r.model_dump() for r in doc.risks],
        "removed_lines": len(doc.raw_text.splitlines()) - len(
            [l for l in doc.safe_text.splitlines() if l != "［该行因命中输入风险检测已移除］"]
        ),
    }


def _normalize_claim(raw: dict) -> dict:
    """给模型可以省略的字段补默认值，再交给 Claim 校验。

    split.py 里本来就有这层兜底（setdefault raw_locator / date_label）——
    模型经常不给这两个字段，缺了就整条报错不合适。
    """
    raw = dict(raw)
    raw.setdefault("id", "c01")
    raw.setdefault("raw_locator", "")
    raw.setdefault("date_label", "")
    raw.setdefault("elements", [])
    raw.setdefault("entities", {})
    raw.setdefault("date_start", None)
    raw.setdefault("date_end", None)
    return raw


def _build_claim(raw: dict):
    """构造并校验一条 Claim，错误转成模型看得懂的提示。"""
    _, _, Claim, _ = _load()
    data = _normalize_claim(raw)
    if data.get("category") not in CATEGORIES:
        raise BridgeError(
            f"category 必须是以下之一：{'、'.join(CATEGORIES)}，收到 {data.get('category')!r}"
        )
    try:
        return Claim(**data)
    except Exception as exc:
        raise BridgeError(f"claim 字段不合法：{exc}") from exc


def cmd_validate_claim(payload: dict) -> dict:
    """把模型拆出来的一条陈述按 open_box 的 Claim 模型校验一遍。

    拆分本身由模型做；这里只保证字段合法、category 在枚举内，
    并回显 elements 与 entities，让模型知道接下来要证明哪些要素。
    """
    claim = _build_claim(_require(payload, "claim"))

    # 要素缺失时按类别补一套默认要素，避免模型漏填导致后面判不出 ok/part
    if not claim.elements:
        claim = type(claim)(**{**claim.model_dump(), "elements": _default_elements(claim)})
    return {"claim": claim.model_dump()}


def _default_elements(claim) -> list[str]:
    """按类别给出"需要单独证明哪些点"的缺省拆分（与 plan.py 的分类口径一致）。"""
    e = claim.entities or {}
    org, dept, role = e.get("org", ""), e.get("dept", ""), e.get("role", "")
    by_cat = {
        "学历": [f"学校={org}", f"院系={dept}", f"专业={e.get('major', '')}"],
        "校内荣誉": [f"授予单位={org}", f"荣誉={dept or claim.raw_text}"],
        "奖学金": [f"授予单位={org}", f"奖项={dept or claim.raw_text}"],
        "学生工作": [f"任职组织={org}", f"职务={dept or role}", "起止时间"],
        "竞赛": [f"赛事={e.get('contest', '')}", f"奖项={e.get('award', '')}", "获奖时间"],
        "论文": [f"论文标题={e.get('title', '')}", f"作者位次={e.get('author_position', '')}",
                 f"刊物={e.get('venue', '')}"],
        "专利": [f"专利名称={e.get('title', '')}", "申请人", "公开号"],
        "开源项目": [f"仓库={e.get('repo', '')}", "贡献者身份"],
        "实习": [f"单位={org}", "岗位", "起止时间"],
        "任职": [f"单位={org}", f"职务={dept or role}", "起止时间"],
    }
    return [x for x in by_cat.get(claim.category, [claim.raw_text]) if not x.endswith("=")]


def cmd_plan(payload: dict) -> dict:
    """把一条陈述展开成检索计划。纯规则，模型不参与。

    返回的不只是查询本身，还有**每条查询为什么存在**（在证哪个要素、第几轮、
    预期拿到什么等级的来源），以及这份计划命中了哪个策略档案、预算多少。
    这样模型拿到的是"情报"而不是"答案"——先发哪几条仍然由它自己决定。
    """
    from app.plan import plan_with_notes, provided_urls

    claim = _build_claim(_require(payload, "claim"))
    name = payload.get("candidate_name", "")
    profile = payload.get("resume_profile") or None      # 模型读出的画像 → 决定用哪套策略
    resume_categories = payload.get("resume_categories") or None

    plan = plan_with_notes(claim, name, profile, resume_categories)

    note = "　".join(plan.notes)
    if claim.category == "学历":
        note = ("学历按设计不检索（学信网不做自动化），应直接判 none 并请候选人授权补证。"
                + ("　" + note if note else ""))
    return {
        "queries": [{
            "text": q.text,
            "site": q.site,
            "kind": q.kind,
            "element": q.element,          # 它在为哪个要素取证（没挂要素的查询不该发）
            "round": q.round,              # 1 定锚 / 2 取证 / 3 补漏
            "weight": q.weight,            # 同轮内优先级，越大越先发
            "purpose": q.purpose,
        } for q in plan.queries],
        # 候选人自提供的链接优先读，不占检索预算；这是拿到公众号内容的唯一合规路径
        "provided_urls": provided_urls(claim),
        "strategy": {"id": plan.profile_id, "name": plan.profile_name},
        "budget": plan.budget,             # 执行层照它走
        "dropped_by_budget": plan.dropped, # 被预算裁掉的，如实报出
        "note": note,
    }


def cmd_classify_tier(payload: dict) -> dict:
    """按规则给一条来源定级 A/B/C/D。定级不交给模型。"""
    judge, _, _, _ = _load()
    tier = judge.classify_tier(
        _require(payload, "url"),
        payload.get("title", ""),
        payload.get("publisher", ""),
        payload.get("snippet", ""),
        organizer_hosts=set(payload.get("organizer_hosts") or []),
        wechat_verified_subject=payload.get("wechat_verified_subject"),
        authoritative_media=bool(payload.get("authoritative_media")),
        is_repost=bool(payload.get("is_repost")),
    )
    return {"source_tier": tier, "meaning": {
        "A": "官方站点上的公示/通知/名单类页面，且不是转载",
        "B": "认证主体公众号文章，或权威媒体报道",
        "C": "平台元数据（GitHub / ORCID / Crossref / OpenAlex / doi.org）",
        "D": "判不出更高等级；也包括官方站点上的部门简介这类非公示页",
    }[tier]}


def _organizer_hosts(claim, payload: dict) -> set[str]:
    """主办方域名：显式传入优先，否则从 claim.entities.organizer_domain 取。

    与 collect.py 的口径一致——赛事组委会的域名决定它的页面能不能算 A 级公示。
    """
    hosts = {h for h in (payload.get("organizer_hosts") or []) if h}
    domain = (claim.entities or {}).get("organizer_domain")
    if domain:
        hosts.add(domain)
    return hosts


def cmd_judge(payload: dict) -> dict:
    """核心：拿一条陈述 + 模型摘来的证据，跑完整的规则判定。

    这里做三件模型不该插手的事：
      1. source_tier 一律由 classify_tier 重算，忽略模型可能给出的等级
      2. identity_score 一律由 WEIGHTS 重算，覆盖模型给出的分值
      3. supports 只认 claim.elements 里真实存在的要素，模型自造的要素被丢弃
    """
    judge, default_next_step, Claim, Evidence = _load()

    claim = _build_claim(_require(payload, "claim"))
    raw_evs = _require(payload, "evidences")
    organizer_hosts = _organizer_hosts(claim, payload)
    valid_elements = set(claim.elements)

    evidences: list[Evidence] = []
    dropped: list[str] = []
    for i, raw in enumerate(raw_evs):
        raw = dict(raw)
        if not raw.get("url"):
            dropped.append(f"第 {i + 1} 条证据缺少 url，已丢弃")
            continue
        # 模型给的身份分与来源等级都不可信，全部重算。
        # is_repost 与 collect.py 口径一致：填了 origin_url 就是转载，转载不算 A 级公示。
        raw.pop("identity_score", None)
        raw["source_tier"] = judge.classify_tier(
            raw["url"],
            raw.get("title", ""),
            raw.get("publisher", ""),
            raw.get("snippet", ""),
            organizer_hosts=organizer_hosts,
            wechat_verified_subject=raw.pop("wechat_verified_subject", None),
            authoritative_media=bool(raw.pop("authoritative_media", False)),
            is_repost=bool(raw.pop("is_repost", False)) or bool(raw.get("origin_url")),
        )
        signals = list(raw.get("identity_signals") or [])
        if raw.pop("candidate_provided", False) and "候选人自提供该链接" not in signals:
            signals.append("候选人自提供该链接")
        raw["identity_signals"] = signals

        # 自造要素一律丢弃
        invented = [s for s in (raw.get("supports") or []) if s not in valid_elements]
        if invented:
            dropped.append(f"第 {i + 1} 条证据引用了不存在的要素，已丢弃：{invented}")
        raw["supports"] = [s for s in (raw.get("supports") or []) if s in valid_elements]
        raw.setdefault("title", "")
        raw.setdefault("publisher", "")
        raw.setdefault("snippet", "")
        raw.setdefault("contradicts", [])
        raw.setdefault("identity_conflicts", [])
        raw.setdefault("accessed_at", "")
        try:
            evidences.append(Evidence(**raw))
        except Exception as exc:
            dropped.append(f"第 {i + 1} 条证据字段不合法，已丢弃：{exc}")

    vc = judge.assess(
        claim, evidences,
        search_exhausted=bool(payload.get("search_exhausted", False)),
        rescore=True,
    )
    vc.next_step = default_next_step(vc)

    valid = judge.valid_evidence(evidences)
    return {
        "verified_claim": vc.model_dump(),
        "dropped": dropped,
        "explain": {
            "valid_evidence_count": len(valid),
            "deduped_count": len(judge.dedupe_by_origin(evidences)),
            "identity_note": (
                "只要有一条 identity_conflicts，无论信号多少一律判 who（身份未确认）"
            ),
            "repost_note": "origin_url 相同的证据只计一次，转载不构成多重证明",
        },
    }


def cmd_summary(payload: dict) -> dict:
    """汇总一份报告的实测覆盖率。数字是算出来的，不要手抄。"""
    from app.schema import STATUS_ORDER, VerifiedClaim

    claims = [VerifiedClaim(**c) for c in _require(payload, "claims")]
    total = len(claims)
    counts = {s: sum(1 for c in claims if c.status == s) for s in STATUS_ORDER}
    elements = sum(len(c.claim.elements) for c in claims)
    proved = sum(len(c.proved) for c in claims)

    def pct(a: int, b: int) -> str:
        return f"{a}/{b}（{a / b * 100:.0f}%）" if b else "—"

    return {
        "total": total,
        "counts": counts,
        "coverage": {
            "found_public_source": pct(sum(1 for c in claims if c.evidence), total),
            "tied_to_candidate": pct(sum(1 for c in claims if c.status in ("ok", "part", "ask")), total),
            "fully_proved": pct(counts["ok"], total),
            "element_level": pct(proved, elements),
            "needs_human": pct(sum(1 for c in claims if c.needs_human), total),
        },
        "disclaimer": (
            "覆盖率只反映公开信息的多少，不代表候选人的可信程度。"
            "本报告不给候选人打分、不做排名、不构成录用建议。"
        ),
    }


COMMANDS = {
    "ingest": cmd_ingest,
    "validate_claim": cmd_validate_claim,
    "plan": cmd_plan,
    "classify_tier": cmd_classify_tier,
    "judge": cmd_judge,
    "summary": cmd_summary,
}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"stdin 不是合法 JSON：{exc}"}, ensure_ascii=False))
        return 0

    cmd = payload.get("cmd")
    fn = COMMANDS.get(cmd)
    if fn is None:
        print(json.dumps({
            "ok": False,
            "error": f"未知命令 {cmd!r}",
            "commands": sorted(COMMANDS),
        }, ensure_ascii=False))
        return 0

    try:
        data = fn(payload)
    except BridgeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 0
    except Exception as exc:  # 规则层的意外错误，如实报出，不吞
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "hint": "确认 open_box 根目录已加入 PYTHONPATH，且 pydantic 等依赖已安装。",
        }, ensure_ascii=False))
        return 1

    print(json.dumps({"ok": True, "data": data}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
