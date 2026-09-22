"""数据模型（第 4 节）。报告以 JSON 落盘，前后端共用这一套结构。"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Category = Literal[
    "学历", "校内荣誉", "奖学金", "学生工作", "竞赛",
    "论文", "专利", "开源项目", "实习", "任职",
]
Tier = Literal["A", "B", "C", "D"]
Status = Literal["ok", "part", "ask", "none", "who"]

STATUS_LABEL: dict[str, str] = {
    "ok": "已证实",
    "part": "部分证实",
    "ask": "待澄清",
    "none": "未找到公开记录",
    "who": "身份未确认",
}

# 展示顺序（统计带与图例都按这个顺序）
STATUS_ORDER = ["ok", "part", "ask", "none", "who"]


# ── 模型输出的类型收敛 ────────────────────────────────────────────────────
# 模型经常不守 schema：该给数组的地方给了单个字符串，该给字符串的地方给了数字。
# 直接交给 Pydantic 会抛 ValidationError，而 Evidence 一旦构造失败，整份报告就
# 跑不出来（collect.py 原先没有 try/except 兜底）。
#
# 注意：**不能把非空字符串静默丢成 []**。identity_conflicts 是"同名一票否决"的
# 唯一依据（judge.decide: 只要有一条 conflict 一律判 who），丢掉会把本该判
# "身份未确认"的条目误升为"已证实"。所以这里是**收敛**（str → [str]），不是丢弃。


def as_str_list(v: Any) -> list[str]:
    """把模型给的任意值收敛成 list[str]，保留信息，绝不因类型不对而丢内容。

    None / "" → []；"某句话" → ["某句话"]；[..] → 逐项转 str 并去空。
    """
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    if isinstance(v, (list, tuple, set)):
        out: list[str] = []
        for x in v:
            out.extend(as_str_list(x))      # 顺带处理嵌套列表
        return out
    if isinstance(v, dict):
        if not v:
            return []
        # 模型偶尔给 {"field": "学院", "diff": "不同"}，拍平成 "学院：不同"
        if "field" in v:
            a = as_str(v.get("field"))
            b = as_str(v.get("diff") or v.get("value") or v.get("detail"))
            joined = f"{a}：{b}" if a and b else (a or b)
            return [joined] if joined else []
        return ["：".join(p for p in (as_str(k), as_str(x)) if p) for k, x in v.items()]
    return [str(v)]


def as_str_or_none(v: Any) -> str | None:
    """非字符串（如模型把年份写成数字 2025）转成字符串，None 保持 None。"""
    if v is None or isinstance(v, str):
        return v
    return str(v)


def as_str(v: Any, default: str = "") -> str:
    """必定给出字符串。用于 title / publisher 这类纯展示字段。"""
    if v is None:
        return default
    if isinstance(v, str):
        return v
    return str(v)


def as_dict(v: Any) -> dict:
    """模型把 entities 写成 JSON 字符串时还原成 dict，其余情况给 {}。"""
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            d = json.loads(v)
        except Exception:                                          # noqa: BLE001
            return {}
        return d if isinstance(d, dict) else {}
    return {}


class Claim(BaseModel):
    """简历里一条独立、可核验的陈述。"""

    id: str
    raw_text: str                 # 简历原文，逐字保留，不改写
    raw_locator: str              # "第2页·教育经历·第3行"
    category: Category
    date_label: str               # 展示用，如 "2024.03"
    date_start: str | None = None  # ISO，缺失为 None
    date_end: str | None = None
    elements: list[str] = Field(default_factory=list)   # 需单独证明的要素，"字段=值"
    entities: dict = Field(default_factory=dict)        # {"org":..,"dept":..,"role":..,"level":..}

    @field_validator("elements", mode="before")
    @classmethod
    def _coerce_elements(cls, v: Any) -> list[str]:
        return as_str_list(v)

    @field_validator("entities", mode="before")
    @classmethod
    def _coerce_entities(cls, v: Any) -> dict:
        return as_dict(v)

    @field_validator("date_label", mode="before")
    @classmethod
    def _coerce_date_label(cls, v: Any) -> str:
        return as_str(v)

    @field_validator("date_start", "date_end", mode="before")
    @classmethod
    def _coerce_dates(cls, v: Any) -> str | None:
        return as_str_or_none(v)


class Evidence(BaseModel):
    """一条公开来源证据。snippet 必须是页面原文逐字摘录。"""

    url: str
    title: str
    publisher: str
    source_tier: Tier
    snippet: str
    published_at: str | None = None
    accessed_at: str = ""
    identity_signals: list[str] = Field(default_factory=list)
    identity_conflicts: list[str] = Field(default_factory=list)
    identity_score: int = 0
    supports: list[str] = Field(default_factory=list)
    contradicts: list[str] = Field(default_factory=list)
    origin_url: str | None = None   # 转载时填原始出处，用于去重

    @field_validator("identity_signals", "identity_conflicts", "supports", "contradicts",
                     mode="before")
    @classmethod
    def _coerce_lists(cls, v: Any) -> list[str]:
        return as_str_list(v)

    @field_validator("title", "publisher", "snippet", mode="before")
    @classmethod
    def _coerce_strs(cls, v: Any) -> str:
        return as_str(v)

    @field_validator("published_at", "origin_url", mode="before")
    @classmethod
    def _coerce_opt_strs(cls, v: Any) -> str | None:
        return as_str_or_none(v)


class VerifiedClaim(BaseModel):
    claim: Claim
    status: Status
    best_tier: Tier | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    proved: list[str] = Field(default_factory=list)
    unproved: list[str] = Field(default_factory=list)
    next_step: str = ""
    question: str | None = None
    needs_human: bool = False
    search_exhausted: bool = False


class InputRisk(BaseModel):
    """输入风险（第 6.1 节）。命中的文本一律不送入 LLM。"""

    kind: Literal["tiny_font", "low_contrast", "offscreen", "injection_pattern"]
    detail: str
    excerpt: str
    locator: str = ""
    action: str = "该段文字已从送入模型的内容中剔除"


class Report(BaseModel):
    id: str
    candidate_name: str
    position: str
    report_no: str
    generated_at: str
    mode: Literal["mock", "live"] = "mock"
    source_file: str = ""
    fictional: bool = False
    fictional_notice: str = ""
    input_risks: list[InputRisk] = Field(default_factory=list)
    claims: list[VerifiedClaim] = Field(default_factory=list)
    disclaimer: str = (
        "本报告仅汇总公开来源与候选人提交的材料，不构成录用决定的唯一依据；"
        "候选人可对任一条结论提交解释与更正。"
    )

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in STATUS_ORDER}
        for vc in self.claims:
            out[vc.status] += 1
        return out
