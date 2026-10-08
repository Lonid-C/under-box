"""步骤 2：陈述拆分（第 6.2 节）。调 LLM 一次，输出 list[Claim]。"""
from __future__ import annotations

import re

from .llm import LLM
from .parse import wrap_untrusted
from .schema import Claim

SYSTEM = """你在把一份简历拆成可以逐条核验的陈述。

规则：
1. raw_text 必须是简历中的原文，逐字复制，不要改写、润色或补全。
2. 一条陈述 = 一段**可以独立核验的经历或成果**，不是一句话里的每个事实。
   一次任职、一个奖项、一篇论文、一件专利、一次学术会议报告、一段实习、一个项目、一次竞赛 = 一条。
   同一段经历里的多个要点（做了什么、用了什么技术、指标提升多少）**留在同一条的
   raw_text 里**，由后续的"要素逐项核对"去分别处理——不要拆成多条。
   反例：把"在 X 公司任算法负责人，主导 Y 项目，收入提升 30%"拆成 5 条，
       会得到 5 条都搜不出独立出处的碎片，还把检索预算放大 5 倍。
3. 每条陈述必须至少含一个**可检索的实体**：机构全称、赛事名、论文标题、专利名称或专利号、
   会议名、仓库名、项目名。
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
9. 总数不超过 20 条。超了先合并非科研条目：同一机构、同一项目的并成一条。
   同一赛事不同年份、国赛/省赛等不同赛段、不同奖项必须各自成条，不得合并；年份和赛级按各条原文分别填写。
   用户会在页面上勾选要核验哪些，但碎片照样搜不出东西——宁可少拆，不要拆碎。
10. **科研成果逐项识别**：每篇论文、每件专利、每次学术会议报告各自一条，不同题目绝不合并，
    也不要并进项目、学历或任职里（"在某课题组发表 2 篇论文"要拆出两篇各自的条目，
    原文只写了篇数、没写题目时整句作一条 论文）。
    · 论文：期刊论文、收进会议论文集的会议论文、预印本（arXiv 等）、在投 / 审稿中 / 已录用的稿件、
      书籍章节都归 论文。原文写了的发表状态（已发表/已录用/在投/预印本）、作者位次
      （第一作者/共同第一作者/通讯作者/第 N 作者）、收录或分级（SCI/EI/CCF-A/中文核心等）、
      DOI、arXiv 编号都要作为要素保留；没写的不要补。
    · 专利：发明专利、实用新型、外观设计、PCT / 国外专利、软件著作权都归 专利。原文写了的
      专利号 / 申请号 / 公开号 / 登记号、类型、状态（已授权/已公开/实审中/已受理）、
      发明人位次、专利权人都要作为要素保留。
    · 会议：在学术会议上做口头报告、墙报（Poster）、特邀报告、担任分会场主席、参会或获会议奖项，
      归 会议。论文已收进会议论文集的归 论文（venue 填会议名），同一篇论文的发表和报告合成一条 论文。
    这三类的 elements 字段名统一用中文，按原文有的写：
      论文：论文标题、刊物、刊物类型、作者位次、发表状态、收录、DOI、arXiv
      专利：专利名称（软著写 软件名称）、专利类型、专利号（或 申请号 / 公开号 / 登记号）、状态、发明人位次、专利权人
      会议：会议名称、报告题目、参与形式、年份、会议奖项
11. 实习（暑期实习、实习生、Intern）一律归 实习，不要归 任职；正式工作、兼职、挂职才归 任职。
12. raw_locator 写原文位置。PDF/文档写"第2页·教育经历·第3行"这类；输入是网页（正文第一行是
    "网页标题："）时没有页码，写"网页·<所在栏目标题>"，如"网页·发表论文"。
    网页里的导航、页脚、"上一篇/下一篇"、访问量这类站点杂项不是经历，跳过。

输出 JSON 对象 {"claims": [Claim, ...]}，不要输出任何解释文字。

Claim 字段：id, raw_text, raw_locator, category, date_label, date_start, date_end, elements, entities
category 只能取：学历 校内荣誉 奖学金 学生工作 竞赛 论文 专利 会议 项目 开源项目 实习 任职
entities 形如 {"org":"某大学","dept":"计算机学院","role":"部长","level":"校级"}
**这些键直接决定检索策略能不能展开，务必填准**：
  GitHub     → github（用户名，从 github.com/<用户名> 里取）、repo（user/repo）
  自提供链接 → provided_urls（简历里出现的个人主页 / 公示链接原样列入）
  竞赛      → contest（赛事全称）、team（队伍名，若有）、role（担任的角色）、
              year（原文明确的比赛年份）、edition（届次）、track（赛道/组别）、
              level（校赛/省赛/区域赛/国赛/国际赛）、award（原文奖级）、
              team_id（队伍编号，若有）、project（获奖作品或项目名，若有）、
              organizer_domain（简历明确给出的赛事/主办方官网域名；没有就不要猜）
  论文      → title（论文标题，逐字）、venue（期刊 / 会议 / 预印本平台名）、
              venue_type（期刊/会议/预印本/书籍）、year、author_order（作者位次原文）、
              status（发表状态原文）、doi、arxiv（arXiv 编号）、
              venue_domain（简历明确给出的期刊/出版社官网域名；没有就不要猜）
  专利      → title（专利或软著名称，逐字）、patent_no（专利号/申请号/公开号/登记号，原样）、
              patent_type（发明/实用新型/外观设计/PCT/软件著作权等原文）、status（原文状态）、
              inventor_order（发明人位次原文）、applicant（专利权人/申请人）
  会议      → conference（会议全称，含届次、年份、缩写原文）、title（报告或墙报题目，若有）、
              form（口头报告/墙报/特邀报告/分会场主席/参会）、year、location（举办地，若有）、
              award（会议奖项，若有）
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
        # JSON 模式只能返回对象：约定键是 claims，模型偶尔换个键名（"陈述"、"items"……），
        # 取第一个"由对象组成的数组"，别因为键名不同就拆出 0 条
        raw = raw.get("claims") if isinstance(raw.get("claims"), list) else next(
            (v for v in raw.values() if isinstance(v, list) and any(isinstance(x, dict) for x in v)), [])

    claims: list[Claim] = []
    LAST_SPLIT.clear()
    LAST_SPLIT.update(given=len(raw or []), dropped=[])
    for i, item in enumerate(raw or [], start=1):
        if not isinstance(item, dict):
            LAST_SPLIT["dropped"].append("不是对象")
            continue
        cat = str(item.get("category") or "").strip()
        if cat not in _CATEGORIES:
            fixed = _CATEGORY_ALIASES.get(cat) or next(
                (v for k, v in _CATEGORY_ALIASES.items() if k in cat), None)
            if fixed:
                item["category"] = fixed
        item.setdefault("id", f"c{i:02d}")
        item.setdefault("raw_locator", "")
        item.setdefault("date_label", "")
        item.setdefault("elements", [])
        item.setdefault("entities", {})
        try:
            claims.append(Claim(**item))
        except Exception as exc:
            # 单条不合规就丢弃，不让一条脏数据毁掉整份报告；原因记下来给进度日志
            loc = ""
            try:
                loc = ".".join(str(x) for x in exc.errors()[0].get("loc", ()))
            except Exception:
                pass
            LAST_SPLIT["dropped"].append(f"{item.get('category') or '无类别'}"
                                         + (f"（{loc} 不合规）" if loc else ""))
            continue
    return add_research_ids(claims)


# 最近一次拆分的统计（模型给了几条、丢了哪些），流水线写进进度日志——
# 拆出 0 条时用户至少知道是模型没给，还是给了但格式不对。
LAST_SPLIT: dict = {}

_CATEGORIES = {"学历", "校内荣誉", "奖学金", "学生工作", "竞赛", "论文", "专利", "会议", "项目",
               "开源项目", "实习", "任职"}
# 模型偶尔用简历栏目名当类别。只收含义确定的别名；拿不准的（如"获奖"）不猜。
_CATEGORY_ALIASES = {
    "教育经历": "学历", "教育背景": "学历", "学位": "学历",
    "发表论文": "论文", "论文发表": "论文", "期刊论文": "论文", "会议论文": "论文", "学术论文": "论文",
    "科研成果": "论文", "预印本": "论文",
    "发明专利": "专利", "软件著作权": "专利", "软著": "专利", "知识产权": "专利",
    "学术会议": "会议", "会议报告": "会议", "学术报告": "会议",
    "科研项目": "项目", "承担项目": "项目", "研究项目": "项目", "项目经历": "项目", "基金项目": "项目",
    "工作经历": "任职", "任职经历": "任职", "工作经验": "任职", "职务": "任职", "社会兼职": "任职",
    "实习经历": "实习",
    "奖学金": "奖学金", "竞赛获奖": "竞赛", "学科竞赛": "竞赛",
}


# ── 规则侧补识别 ─────────────────────────────────────────────────────────
# 编号类信息（DOI、arXiv 编号、专利号）格式固定，正则比模型稳。模型漏填 entities 时
# 从原文补上，只作检索线索，**不新增待证明要素**——要素仍以原文 / 模型拆出的为准。

_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s，。；;、,\"'<>（）()\[\]]+)", re.I)
_ARXIV = re.compile(r"(?:arxiv(?:\.org/(?:abs|pdf)/|\s*[:：]?\s*))(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
_PATENT_NO = re.compile(
    r"(CN\s?\d{9,12}\s?[ABUSY]\d?"                    # 中国公开 / 授权公告号
    r"|ZL\s?\d{8,12}\s?\.\s?[\dX]"                   # 授权专利号（ZL + 申请号）
    r"|(?<![\d.])\d{12}\s?\.\s?[\dX](?![\d])"         # 申请号 2021 1 0123456.7
    r"|(?:US|EP|JP|KR)\s?\d[\d,]{6,10}\s?[AB]\d?"    # 国外
    r"|WO\s?\d{4}\s?/\s?\d{6}"                        # PCT 国际公布号
    r"|\d{4}SR\d{6,7})",                             # 软件著作权登记号
    re.I)
_INTERN = re.compile(r"实习|\bintern(?:ship)?\b", re.I)


def _blob(c: Claim) -> str:
    return " ".join([c.raw_text or "", *(c.elements or [])])


_EMPTY_VALUES = {"", "无", "未知", "未提及", "未注明", "未写", "不详", "null", "none", "n/a", "—", "-"}


def add_research_ids(claims: list[Claim]) -> list[Claim]:
    out = []
    for c in claims:
        ent = dict(c.entities or {})
        blob = _blob(c)
        update: dict = {}
        # 模型有时把"原文没写"的字段也列成要素（"DOI=无"），那不是需要证明的点
        elements = [e for e in (c.elements or [])
                    if "=" not in e or e.split("=", 1)[1].strip().lower() not in _EMPTY_VALUES]
        if elements != list(c.elements or []):
            update["elements"] = elements
        if c.category == "论文":
            # 只认真正的 DOI（10. 开头）；EI 检索号、期刊编号被模型填成 DOI 时不拿去查
            doi = ent.get("doi") if isinstance(ent.get("doi"), str) else ""
            if doi and not re.match(r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?10\.\d{4,9}/", doi.strip(), re.I):
                ent.pop("doi", None)
            if not ent.get("doi") and (m := _DOI.search(blob)):
                ent["doi"] = m.group(1).rstrip(".")
            if not ent.get("arxiv") and (m := _ARXIV.search(blob)):
                ent["arxiv"] = m.group(1)
        elif c.category == "专利":
            if not ent.get("patent_no") and (m := _PATENT_NO.search(blob)):
                ent["patent_no"] = re.sub(r"[\s,]+", "", m.group(1)).upper()
        elif c.category == "任职":
            # 实习不进入核验（见 pipeline.skip_categories）；模型偶尔把实习归成任职，
            # 那样它会被当成正式任职去检索。原文或岗位里明说实习的，改回实习。
            role = ent.get("role") if isinstance(ent.get("role"), str) else ""
            if _INTERN.search((c.raw_text or "") + " " + role):
                update["category"] = "实习"
        if ent != (c.entities or {}):
            update["entities"] = ent
        out.append(c.model_copy(update=update) if update else c)
    return out
