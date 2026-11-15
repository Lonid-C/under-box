"""步骤 6：澄清问题生成（第 6.8 节）。只对 ask/part/none/who 生成。"""
from __future__ import annotations

from .llm import LLM
from .schema import STATUS_LABEL, VerifiedClaim

SYSTEM = """为下面这条待澄清的经历，写一句面试官可以直接问出口的问题。

陈述：{raw_text}
状态：{status_label}
已证明：{proved}
尚未证明：{unproved}
具体差异：{contradicts}

要求：
- 一句话，不超过 45 字，问号结尾。
- 陈述事实差异，不作指控。写"公告显示为副部长"，不要写"你虚报了职务"。
- 给候选人留出解释空间：可能是后续接任、口径不同、材料未公开。
- 未找到公开记录的，问的是能否补充材料，不要暗示造假。

只输出 JSON：{{"question": "……？"}}"""

NEEDS_QUESTION = ("ask", "part", "none", "who")


def generate_question(vc: VerifiedClaim, llm: LLM) -> str | None:
    if vc.status not in NEEDS_QUESTION:
        return None
    contradicts = [c for e in vc.evidence for c in e.contradicts]
    system = SYSTEM.format(
        raw_text=vc.claim.raw_text,
        status_label=STATUS_LABEL[vc.status],
        proved=vc.proved or "无",
        unproved=vc.unproved or "无",
        contradicts=contradicts or "无",
    )
    try:
        got = llm.complete_json(system, vc.claim.raw_text)
    except Exception:
        return None
    if isinstance(got, dict):
        q = (got.get("question") or "").strip()
    elif isinstance(got, str):
        q = got.strip()
    else:
        return None
    return q[:60] if q else None


def default_next_step(vc: VerifiedClaim) -> str:
    """LLM 不可用时的兜底下一步，措辞一律中性。"""
    return {
        "ok": "公开来源可直接采信，无需候选人补充材料。",
        "part": "部分要素已证实，其余要素公开来源不足，建议在面试中了解或请候选人补充材料。",
        "ask": "发现具体不一致，请候选人说明后再决定如何记录，不要据此直接下结论。",
        "none": "公开检索无结果不指向任何问题，可请候选人自行提供证明材料。",
        "who": "找到的记录标识与候选人不符，不能计入本人，需人工复核或请候选人补充佐证。",
    }[vc.status]
