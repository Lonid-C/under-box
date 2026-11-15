"""步骤 2.5：从简历里读出画像——**这一步才是模型该干的活**。

画像（identity / industries / level / skills）决定用哪套检索策略。它需要"理解"——
判断这是一份在校生简历还是金融高管简历，是模型擅长的；而"用哪套策略"
（`data/strategies.json`）和"这条算不算证明了"（`judge.py`）是规则的事。

纪律：
  · 只取值于枚举内的值。模型给了清单外的值 → 当作没答，走默认（unknown）→ 兜底档案。
  · **不猜**。判断不出来就 unknown，兜底档案的预算更低——广撒网只换噪音。
  · 这是唯一一处模型输出会影响检索计划的地方，但它影响的是"去哪搜"，不是"算不算证明"。
"""
from __future__ import annotations

import json
import re

from .llm import LLM, LLMTransient, LLMUnavailable
from .strategy import ResumeProfile

IDENTITIES = ("student", "professional", "researcher", "creator", "public_sector")
INDUSTRIES = ("internet", "software", "finance", "healthcare", "education",
              "manufacturing", "legal", "media", "gov")
LEVELS = ("intern", "entry", "mid", "senior", "lead", "exec")

SYSTEM = """你是履历核验流水线里的画像归纳步骤。读一份简历的正文，判断候选人的画像。

只输出一个 JSON 对象，不要输出任何解释文字：
{"name": "...", "identity": "...", "industries": ["..."], "level": "...", "skills": ["..."]}

name（必填）：候选人姓名，**用简历里写明的原词**（如有英文名一并保留，如"张三 Leonid ZHANG"）。

identity（必填，只能一个）：
  student        在校生 / 应届生 / 实习生
  professional   职场人（企业任职）
  researcher     高校教师 / 科研人员
  creator        自由职业 / 独立开发者 / 创作者
  public_sector  体制内 / 事业单位 / 国企

industries（可多个，取值只能是）：
  internet software finance healthcare education manufacturing legal media gov

level（必填，只能一个）：
  intern 实习/在校    entry 毕业 0-3 年    mid 3-8 年
  senior 8 年以上     lead 团队负责人      exec 总监及以上

skills（5–12 个）：从简历里抽**可验证的专有名词**——技术栈、工具、证书、
  竞赛全称、系统名。宁缺勿滥：一个编造出来的技能名会变成一条浪费预算的查询。"""

USER = "简历正文：\n\n{body}\n\n请输出画像 JSON。"


def _clean(value, allowed: tuple[str, ...], default):
    """枚举收敛：不在清单里的值当作没答。**别猜**。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return default
    out = [v for v in value if isinstance(v, str) and v.strip() in allowed]
    return out or default


def derive_profile(text: str, llm: LLM) -> ResumeProfile:
    """读简历 → 画像。任何异常都落到 unknown（兜底档案），**绝不抛错**：
    画像失败不该让整条流水线挂掉，它只该让检索保守一点。"""
    body = text.strip()[:9000]
    if not body:
        return ResumeProfile()
    try:
        data = llm.complete_json(SYSTEM, USER.format(body=body), max_tokens=900)
    except (LLMUnavailable, LLMTransient):
        # 模型不可用/超时 → 画像归 unknown，走兜底档案（预算更低）。
        # **绝不抛错**：画像失败不该让整条流水线挂掉，它只该让检索保守一点。
        return ResumeProfile()

    if not isinstance(data, dict):
        return ResumeProfile()

    identity = data.get("identity")
    identity = identity if isinstance(identity, str) and identity in IDENTITIES else "unknown"
    industries = _clean(data.get("industries"), INDUSTRIES, [])
    level = data.get("level")
    level = level if isinstance(level, str) and level in LEVELS else "unknown"

    skills = data.get("skills")
    skills = [s.strip() for s in skills if isinstance(s, str) and s.strip()] if isinstance(skills, list) else []
    # 技能不做枚举收敛（它是自由文本），但要截断——一个失控的列表会烧掉检索预算
    skills = list(dict.fromkeys(skills))[:12]

    name = data.get("name")
    # 姓名只做长度/字符的粗校验：太短或带标点的多半不是姓名，宁可空着
    name = name.strip() if isinstance(name, str) else ""
    if not (2 <= len(name) <= 30) or re.search(r"[，。；、！？\s]{2,}", name):
        name = ""

    return ResumeProfile(identity=identity, industries=industries, level=level,
                         skills=skills, name=name)
