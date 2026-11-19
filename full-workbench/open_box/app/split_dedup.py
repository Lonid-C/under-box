"""Consolidate repeated excerpts while keeping separately verifiable facts."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .academic import stages_in
from .schema import Claim


_FIELDS = {
    "学历": {"topic": ("school", "university", "org", "学校", "院校"),
           "stage": ("stage", "degree", "学段", "学位", "学历层次", "培养层次"),
           "major": ("major", "专业")},
    "竞赛": {"topic": ("contest", "赛事", "赛事名称", "比赛", "竞赛"),
           "school": ("school", "学校"), "region": ("province", "region", "省份", "赛区"),
           "award": ("award", "奖级", "奖项", "奖项名称", "获奖级别"),
           "level": ("level", "赛级", "赛段", "赛事级别", "阶段"),
           "track": ("track", "赛道", "组别"), "edition": ("edition", "届次"),
           "team": ("team", "team_id", "team_number", "队伍", "队伍编号"),
           "project": ("project", "项目", "获奖作品", "项目名称", "作品名称")},
    "奖学金": {"topic": ("award", "奖项", "奖学金", "荣誉", "荣誉名称", "奖项名称"),
            "school": ("school", "学校", "院校", "org"), "issuer": ("授予单位", "颁发单位"),
            "stage": ("获奖学段", "学段"), "level": ("level", "级别", "奖级")},
    "论文": {"topic": ("title", "论文标题", "论文题目", "题目"),
           "identifier": ("doi", "arxiv", "DOI", "arXiv"),
           "venue": ("venue", "刊物", "期刊", "会议")},
    "专利": {"topic": ("title", "专利名称", "软件名称"),
           "identifier": ("patent_no", "专利号", "申请号", "公开号", "登记号")},
    "会议": {"topic": ("conference", "会议名称"), "title": ("title", "报告题目"),
           "role": ("form", "参与形式", "会议奖项")},
    "项目": {"topic": ("title", "project", "项目名称", "项目"),
           "identifier": ("project_no", "project_id", "grant_no", "项目编号", "课题编号", "基金编号")},
    "开源项目": {"topic": ("title", "项目名称", "项目"), "repo": ("repo", "仓库", "仓库路径")},
    "任职": {"topic": ("company", "org", "公司", "机构", "任职组织", "单位"),
           "role": ("role", "岗位", "职务", "职位"), "department": ("dept", "部门", "院系")},
    "学生工作": {"topic": ("org", "任职组织", "组织", "机构"),
             "role": ("role", "岗位", "职务", "职称")},
    "著作": {"topic": ("title", "书名"), "identifier": ("isbn", "ISBN"), "count": ("出版数量",)},
    "交流活动": {"topic": ("title", "program", "项目/身份", "交流项目", "活动名称", "项目名称")},
    "教学活动": {"topic": ("title", "活动名称", "课程名称")},
    "艺术成果": {"topic": ("title", "作品/演出", "作品名称", "活动名称")},
}
_FIELDS["校内荣誉"] = _FIELDS["奖学金"]
_FIELDS["实习"] = _FIELDS["任职"]
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_MONTH = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*[./年-]\s*(1[0-2]|0?[1-9])(?!\d)")


def _months(value: str) -> set[str]:
    return {f"{year}-{int(month):02d}" for year, month in _MONTH.findall(value)}


def _key(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold()).strip("。；;，,《》‘’“”\"'")


def _pairs(claim: Claim) -> list[tuple[str, str]]:
    pairs = [(key, str(value)) for key, value in claim.entities.items() if isinstance(value, (str, int))]
    for element in claim.elements:
        parts = re.split(r"[=：:]", element, maxsplit=1)
        if len(parts) == 2:
            pairs.append((parts[0].strip(), parts[1].strip()))
    return pairs


@dataclass
class _Excerpt:
    claim: Claim
    raw: str
    section: int
    fields: dict[str, set[str]]

    @classmethod
    def build(cls, claim: Claim, section: int):
        original_text = unicodedata.normalize("NFKC", claim.raw_text)
        raw = _key(claim.raw_text)
        pairs = _pairs(claim)
        aliases = _FIELDS.get(claim.category, {})
        fields = {name: {value for key, original in pairs if key in names
                         and (value := _key(original)) and value in raw}
                  for name, names in aliases.items()}
        if fields.get("stage"):
            fields["stage"] = stages_in(" ".join(fields["stage"])) or fields["stage"]
        # The narrower title/award reported in a separate field takes priority
        # over guessing an event from a paragraph containing several facts.
        if claim.category == "校内荣誉" and fields["topic"] and all("奖学金" in word for word in fields["topic"]):
            elements = [re.sub(r"^荣誉(?:名称)?=", "奖项=", item) for item in claim.elements]
            claim = claim.model_copy(update={"category": "奖学金", "elements": elements})
        times = " ".join([claim.date_label, claim.date_start or "", claim.date_end or "",
                          *(value for key, value in pairs if key == "year" or re.search("年份|年度|时间|日期", key))])
        years = set(_YEAR.findall(original_text))
        fields["year"] = years.intersection(_YEAR.findall(times)) or years
        known_months = _months(original_text)
        fields["month"] = known_months.intersection(_months(times)) or known_months
        return cls(claim, raw, section, fields)


def _same_fact(left: _Excerpt, right: _Excerpt) -> bool:
    if left.claim.category != right.claim.category:
        return False
    shared = {key for key, values in left.fields.items() if values & right.fields.get(key, set())}
    if any(values and right.fields.get(key) and key not in shared for key, values in left.fields.items()):
        return False
    if left.raw == right.raw:
        return True
    short, long = sorted((left, right), key=lambda item: len(item.raw))
    if short.raw in long.raw and len(short.raw) >= 8:
        if any(short.fields.get(field) for field in ("topic", "identifier", "repo")):
            return True
        if left.section == right.section and len(short.raw) * 2 >= len(long.raw):
            return True
    # A registered identifier can connect a repeated summary to its detailed entry.
    if "identifier" in shared:
        return True
    if not {"topic", "year"} <= shared:
        return False
    category = left.claim.category
    if category == "竞赛":
        return bool(shared & {"award", "team", "project"} and shared & {"level", "track", "team", "project"})
    if category == "学历":
        return "stage" in shared
    if category in {"奖学金", "校内荣誉"}:
        return True
    if category in {"实习", "任职", "学生工作"}:
        return {"role", "month"} <= shared
    return category == "论文" and "venue" in shared


def _merge(left: _Excerpt, right: _Excerpt) -> _Excerpt:
    # Keep one actual original excerpt and its locator. Never concatenate two
    # passages into a quote that did not occur in the resume.
    def completeness(entry):
        grounded = sum(value in entry.raw for element in entry.claim.elements
                       if (value := _key(element.partition("=")[2])))
        return len(entry.raw), grounded

    main, other = sorted((left, right), key=completeness, reverse=True)
    elements = {}
    for part in (*main.claim.elements, *other.claim.elements):
        label, _, original = part.partition("=")
        if part in main.claim.elements or (_key(original) and _key(original) in main.raw):
            elements.setdefault((_key(label), _key(original)), part)
    entities = dict(main.claim.entities)
    for key, value in other.claim.entities.items():
        if not entities.get(key) and isinstance(value, str) and _key(value) and _key(value) in main.raw:
            entities[key] = value
    links = other.claim.entities.get("provided_urls")
    if isinstance(links, list):
        observed = [url for url in links if isinstance(url, str) and url and url in main.claim.raw_text]
        existing = entities.get("provided_urls")
        if observed and (not existing or isinstance(existing, list)):
            entities["provided_urls"] = list(dict.fromkeys([*(existing or []), *observed]))
    update = {"elements": list(elements.values()), "entities": entities}
    for field in ("date_label", "date_start", "date_end"):
        value = getattr(other.claim, field)
        if not getattr(main.claim, field) and value and _key(value) in main.raw:
            update[field] = value
    return _Excerpt.build(main.claim.model_copy(update=update), main.section)


def consolidate(excerpts: list[tuple[Claim, int]]) -> list[Claim]:
    unique: list[_Excerpt] = []
    for claim, section in excerpts:
        current = _Excerpt.build(claim, section)
        matches = [i for i, saved in enumerate(unique) if _same_fact(saved, current)]
        if len(matches) == 1:
            index = matches[0]
            unique[index] = _merge(unique[index], current)
        else:
            unique.append(current)
    return [item.claim.model_copy(update={"id": f"c{number:02d}"}) for number, item in enumerate(unique, 1)]
