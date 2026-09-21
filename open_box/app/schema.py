"""数据模型（第 4 节）。报告以 JSON 落盘，前后端共用这一套结构。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

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
