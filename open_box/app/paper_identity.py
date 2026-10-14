"""论文登记中的作者联合关联。拼音不是独立身份凭证，仍转人工确认。"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse, unquote

from .identity import matches_school
from .schema import Evidence


def compact(value):
    return re.sub(r"[^a-z0-9\u3400-\u9fff]", "", unicodedata.normalize("NFKD", str(value or "")).lower())


def latin_names(name):
    """全名用于定位，不扩展首字母；多音姓缺少明示英文名时保持未知。"""
    if re.fullmatch(r"[\u3400-\u9fff]{2,5}", name or ""):
        if name[0] in "单曾仇解乐区查翟":
            return []
        try:
            from pypinyin import lazy_pinyin
            compound = name[:2] in {"欧阳", "司马", "上官", "诸葛", "东方", "皇甫", "尉迟", "夏侯", "公孙", "慕容"}
            n = 2 if compound else 1
            family = "".join(lazy_pinyin(name[:n])); given = "".join(lazy_pinyin(name[n:]))
            return [given + " " + family, family + " " + given] if given else []
        except ImportError:
            return []
    tokens = re.findall(r"[A-Za-z][A-Za-z'-]+", name or "")
    return [" ".join(tokens), " ".join(tokens[::-1])] if len(tokens) >= 2 else []


def _doi(value):
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", str(value or ""), flags=re.I)
    return value.lower().strip() if re.fullmatch(r"10\.\d{4,9}/\S+", value) else ""


def _registry(url, doi):
    if not isinstance(url, str): return False
    p = urlparse(url)
    if p.scheme != "https" or p.username or p.password or p.query or p.fragment:
        return False
    if p.hostname == "api.crossref.org":
        return unquote(p.path).lower() == "/works/" + doi
    return p.hostname == "api.openalex.org" and bool(re.fullmatch(r"/works/W\d+", p.path))


def _citation_name(raw, row):
    """输入必须确实提及本作者全名或文献缩写，不凭拼音给无关论文补作者。"""
    if compact(row["name"]) in compact(raw):
        return True
    tokens = row["name"].split()
    family = row.get("family") or (tokens[-1] if tokens else "")
    given = row.get("given") or (tokens[0] if len(tokens) > 1 else "")
    if not family or not given:
        return False
    return bool(re.search(r"(?<![A-Za-z])" + re.escape(family) + r"\s*,?\s*" + re.escape(given[0])
                          + r"(?:\.|#|\*|\s*[,，;；])", raw or "", re.I))


def author_match(claim, record):
    from .person_scope import PersonCheck
    if claim.category != "论文" or not isinstance(record, dict):
        return PersonCheck(False, reason="不是论文登记记录")
    ent = claim.entities or {}; context = ent.get("candidate_identity_context") or {}
    name = ent.get("candidate_name") or context.get("candidate_name") or ""
    title = ent.get("title") or next((e.partition("=")[2] for e in claim.elements if e.startswith("论文标题=")), "")
    doi = _doi(record.get("doi")); sources = record.get("sources") or []
    rows = record.get("authors") or []
    if (not isinstance(sources, list) or not isinstance(rows, list)
            or not all(isinstance(r, dict) and isinstance(r.get("name"), str)
                       and isinstance(r.get("institutions", []), list)
                       and all(isinstance(i, str) for i in r.get("institutions", []))
                       and isinstance(r.get("institution_records", []), list)
                       and all(isinstance(i, dict) for i in r.get("institution_records", [])) for r in rows)):
        return PersonCheck(False, reason="登记字段格式无效")
    if (not doi or not sources or not all(_registry(u, doi) for u in sources)
            or not title or compact(title) != compact(record.get("title"))
            or compact(title) not in compact(claim.raw_text)):
        return PersonCheck(False, reason="登记出处、DOI或原文题名不一致")
    wanted_doi = ent.get("doi")
    if wanted_doi and _doi(wanted_doi) != doi:
        return PersonCheck(False, reason="DOI与本条输入不一致")
    venue = ent.get("venue") or next((e.partition("=")[2] for e in claim.elements if e.startswith("刊物=")), "")
    if venue and compact(venue) != compact(record.get("venue")):
        return PersonCheck(False, reason="同题作品刊物不一致")
    aliases = {compact(v) for v in latin_names(name)}
    matches = [r for r in rows if compact(r.get("name")) in aliases or r.get("name") == name]
    if len(matches) != 1:
        return PersonCheck(False, reason="未找到唯一的完整作者姓名")
    row = matches[0]
    orgs = [*context.get("education_schools", []), *context.get("current_employers", [])]
    from .plan import school_domain
    from .search import url_in_domain
    def institution_matches(org):
        if any(matches_school(org, inst) for inst in row.get("institutions", [])): return True
        expected = school_domain(org)
        if not expected: return False
        for institution in row.get("institution_records", []):
            rid = urlparse(institution.get("ror") or "")
            if (rid.netloc != "ror.org" or rid.scheme != "https" or rid.query or rid.fragment
                    or not re.fullmatch(r"/[a-z0-9]{9}", rid.path)
                    or institution.get("source") != "https://api.ror.org/v2/organizations" + rid.path): continue
            if any(isinstance(d, str) and re.fullmatch(r"[a-z0-9.-]+", d)
                   and url_in_domain("https://" + d, expected) for d in institution.get("domains", [])): return True
        return False
    if not any(institution_matches(org) for org in orgs):
        return PersonCheck(False, reason="本人作者行的单位不对应，不能借同篇其他作者的单位")
    # 姓名 + 本人单位 + 本篇明确署名，中文拼音还必须有原文明示的同篇合作者。
    if not _citation_name(claim.raw_text, row):
        return PersonCheck(False, reason="输入未明确提及这个作者")
    if re.search(r"[\u3400-\u9fff]", name):
        colleagues = [r for r in rows if r is not row and _citation_name(claim.raw_text, r)]
        if not colleagues:
            return PersonCheck(False, reason="拼音匹配缺少同篇合作者交叉关联")
    return PersonCheck(True, ["英文作者联合关联"])


def supports_for(claim, record):
    if not author_match(claim, record).allowed:
        return []
    name = claim.entities.get("candidate_name") or (claim.entities.get("candidate_identity_context") or {}).get("candidate_name", "")
    aliases = {compact(v) for v in latin_names(name)}
    row = next(r for r in record["authors"] if compact(r["name"]) in aliases or r["name"] == name)
    result = []
    for element in claim.elements:
        field, _, value = element.partition("="); value = value.strip()
        if field.strip() in {"论文标题", "论文题目", "题目"} and compact(value) == compact(record["title"]):
            result.append(element)
        elif field.strip() in {"刊物", "期刊", "会议"} and compact(value) == compact(record.get("venue")):
            result.append(element)
        elif field.strip() == "DOI" and _doi(value) == record["doi"]:
            result.append(element)
        elif field.strip() in {"年份", "发表年份"} and value.rstrip("年") == record.get("year"):
            result.append(element)
        elif field.strip() == "刊物类型" and value == "期刊" and record.get("type") == "journal-article":
            result.append(element)
        elif field.strip() == "作者位次" and ((value == "第一作者" and row.get("position") == 1)
                or re.fullmatch(r"第\d+作者", value) and int(re.findall(r"\d+", value)[0]) == row.get("position")):
            result.append(element)
    return result


def from_hit(claim, hit):
    if not hit.author_records or not hit.registry_sources:
        return None
    doi = _doi(hit.url)
    record = {"title": hit.title, "doi": doi, "venue": hit.venue, "year": hit.year,
              "type": hit.publication_type, "authors": list(hit.author_records), "sources": list(hit.registry_sources)}
    checked = author_match(claim, record)
    if not checked.allowed:
        return None
    lines = ["论文登记题名：" + hit.title, "DOI：" + doi, "刊物：" + hit.venue, "发表年份：" + hit.year]
    lines += ["公开登记来源：" + url for url in hit.registry_sources]
    lines += ["作者 " + str(r.get("position")) + "：" + r["name"] + "；单位：" + "、".join(r.get("institutions", [])) for r in hit.author_records]
    from .provenance import bind_structured
    return bind_structured(Evidence(url=hit.registry_sources[0], title=hit.title, publisher="Crossref / OpenAlex 公开登记",
                    source_tier="C", snippet="\n".join(lines), identity_signals=checked.signals,
                    identity_score=3, supports=supports_for(claim, record), paper_record=record,
                    extraction_method="text", published_at=hit.year or None))
