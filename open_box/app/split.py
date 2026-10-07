"""步骤 2：陈述拆分（第 6.2 节）。调 LLM 一次，输出 list[Claim]。"""
from __future__ import annotations

from .llm import LLM
from .parse import wrap_untrusted
from .schema import Claim

SYSTEM = """你在把一份简历拆成可以逐条核验的陈述。

规则：
1. raw_text 必须是简历中的原文，逐字复制，不要改写、润色或补全。
2. 一条陈述 = 一段**可以独立核验的经历或成果**，不是一句话里的每个事实。
   一次任职、一个奖项、一篇论文、一段实习、一个项目、一次竞赛 = 一条。
   同一段经历里的多个要点（做了什么、用了什么技术、指标提升多少）**留在同一条的
   raw_text 里**，由后续的"要素逐项核对"去分别处理——不要拆成多条。
   反例：把"在 X 公司任算法负责人，主导 Y 项目，收入提升 30%"拆成 5 条，
       会得到 5 条都搜不出独立出处的碎片，还把检索预算放大 5 倍。
3. 每条陈述必须至少含一个**可检索的实体**：机构全称、赛事名、论文标题、仓库名、项目名。
   纯过程描述（"构建了数据管线""把召回率从 69.8% 提到 82.5%"）不构成独立陈述——
   它们属于所属经历那条的 raw_text。
4. elements 列出这条陈述里每个需要单独证明的点，用"字段=值"的形式。
5. **学历这条放学校、专业、学位、就读时间以及明确自述的入学方式**（GPA、语言成绩可以跟着学历走）。
   简历明确写保送、推免或统考入学时，必须把"入学方式=保送/推免/统考"作为单独要素；
   找到接收学校的同名在读记录，只证明对应就读要素，不代表入学方式也已证明。
   奖学金、荣誉、竞赛**必须各自成条**——它们有独立的公示记录，
   并进学历就永远不会被检索（学历按设计不检索）。
6. 模糊表达保持模糊，不要替候选人下定论。
   简历写"参与某项目"就是"参与"，不要写成"负责"。
7. 只拆职业与学业事实。婚恋、健康、宗教、政治、家庭等内容一律跳过，不要出现在输出里。
8. 无法判断日期时 date_start/date_end 填 null，不要猜测。
9. 总数不超过 12 条。超了就合并：同一机构、同一赛事、同一项目的并成一条。
   宁可少拆——把预算摊薄到一堆搜不出结果的碎片上，等于什么都没搜。

按 Claim 的 JSON schema 输出数组，不要输出任何解释文字。

Claim 字段：id, raw_text, raw_locator, category, date_label, date_start, date_end, elements, entities
category 只能取：学历 校内荣誉 奖学金 学生工作 竞赛 论文 专利 项目 开源项目 实习 任职
entities 形如 {"org":"某大学","dept":"计算机学院","role":"部长","level":"校级"}
**这些键直接决定检索策略能不能展开，务必填准**：
  GitHub     → github（用户名，从 github.com/<用户名> 里取）、repo（user/repo）
  自提供链接 → provided_urls（简历里出现的个人主页 / 公示链接原样列入）
  竞赛      → contest（赛事全称）、team（队伍名，若有）、role（担任的角色）、
              year（原文明确的比赛年份）、edition（届次）、track（赛道/组别）、
              level（校赛/省赛/区域赛/国赛/国际赛）、award（原文奖级）、
              team_id（队伍编号，若有）、project（获奖作品或项目名，若有）、
              organizer_domain（简历明确给出的赛事/主办方官网域名；没有就不要猜）
  论文      → title（论文标题）、venue（刊物 / 会议）、
              venue_domain（简历明确给出的期刊/出版社官网域名；没有就不要猜）
  项目      → title（项目名称）；没有公开仓库或开源声明时使用本类
  开源项目  → repo（仓库路径，形如 user/repo）、title（项目名称）；仅限明确开源的项目
  实习/任职 → company（公司全称）、role（岗位）
  校内/学历 → org（机构全称）、dept（院系）
  凡本条明确提到所属学校（含竞赛/奖项/学生组织），另填 school（学校本名，去掉院系/学生会后缀）；不要把主办方当学校，也不要从另一条经历猜所属学校。
**两个不同的赛事、两家不同的公司，各自成一条，不要合并**——合并后的
"赛事A / 赛事B"没法作为检索词。
竞赛奖项必须同时保留赛事、年份/届次、赛道/组别、阶段和奖级这些原文已写的要素；
没有写的不要补。指导教师奖/组织奖不等于学生个人奖；团队获奖不等于本人已被证明是成员。
MCM/ICM、校赛/省赛/国赛、模拟赛/正式赛、初稿公示/正式名单的区别须保留；英文奖级原样保留，不能自行换算成中国一二等奖。
“全国高等院校英语能力大赛/高等院校大学生英语能力大赛”（eaedu）与“全国大学生英语竞赛”（NECCS）是不同赛事，不能互相改名。
“高教社杯”是冠名，多个赛事共用；只有原文含数学建模/数模或成图等上下文时才能识别具体赛事，只有冠名时保留原词。"""


def split_claims(resume_text: str, llm: LLM) -> list[Claim]:
    """resume_text 必须是已经过 parse.scrub 的安全文本。

    max_tokens 给到 12000：真实简历动辄拆出 20+ 条陈述，fixture 只有 11 条所以
    默认的 4096 从来没露馅。max_tokens 只是上限不是成本（按真实输出计费），
    放开只防截断。
    """
    raw = llm.complete_json(SYSTEM, wrap_untrusted(resume_text), max_tokens=12000)
    if isinstance(raw, dict):
        raw = raw.get("claims", [])

    claims: list[Claim] = []
    for i, item in enumerate(raw or [], start=1):
        item.setdefault("id", f"c{i:02d}")
        item.setdefault("raw_locator", "")
        item.setdefault("date_label", "")
        item.setdefault("elements", [])
        item.setdefault("entities", {})
        try:
            claims.append(Claim(**item))
        except Exception:
            continue          # 单条不合规就丢弃，不让一条脏数据毁掉整份报告
    return claims
