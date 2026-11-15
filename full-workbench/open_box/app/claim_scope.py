"""拆开事实、本人角色、范围、修饰语与指标；不凭简历写法产生负面标签。"""
from __future__ import annotations
import re
from urllib.parse import urlsplit

PROJECT_CATEGORIES = {'项目', '开源项目'}
ROLES = re.compile(r'主导|牵头|独立开发|独立完成|核心贡献者|核心开发者|负责|参与|贡献|\b(?:core contributor|core developer|lead developer|sole developer|independent developer|participated|contributed|lead|led|maintainer|contributor|owner)\b', re.I)
MODIFIERS = re.compile(r'自研|自主研发|从零实现|从零开发|核心架构|核心模块|全链路|端到端|\b(?:self[- ]developed|from scratch|independently|core architecture|core module|end[- ]to[- ]end)\b', re.I)
METRICS = re.compile(r'(?:性能|吞吐量|吞吐|延迟|耗时|准确率|成本|效率|速度)[^。；;\n]{0,24}?(?:提升|提高|降低|下降|减少|增长)[^。；;\n]{0,10}?\d+(?:\.\d+)?\s*(?:%|％|个百分点|倍)', re.I)


def scope_parts(claim) -> dict | None:
    raw=claim.raw_text or ''
    roles=list(dict.fromkeys(m.group() for m in ROLES.finditer(raw)))
    modifiers=list(dict.fromkeys(m.group() for m in MODIFIERS.finditer(raw)))
    metrics=list(dict.fromkeys(m.group() for m in METRICS.finditer(raw)))
    if not (roles or modifiers or metrics) or claim.category not in PROJECT_CATEGORIES | {'任职','实习'}:
        return None
    scope=[e.partition('=')[2] for e in claim.elements if e.partition('=')[0] in {'工作范围','负责模块','模块'}]
    # 只有原文确有这个范围才保留，不让模型补出全链路。
    scope=[s for s in scope if s and s in raw]
    return {'facts':[e for e in claim.elements if e.partition('=')[0] not in {'个人角色','工作范围','修饰语','量化指标','角色','职务'}],
            'roles':roles,'scope':scope,'modifiers':modifiers,'metrics':metrics}


def enrich_claim(claim):
    parts=scope_parts(claim)
    from .strategy import github_trigger
    links=github_trigger(claim)
    if not parts and not links: return claim
    parts=parts or {"roles":[],"scope":[],"modifiers":[],"metrics":[]}
    elements=list(claim.elements)
    if links:
        for p in links:
            if '关联项目链接='+p['url'] not in elements: elements.append('关联项目链接='+p['url'])
        if '本人参与=该项目' not in elements: elements.append('本人参与=该项目')
    for key,values in [('个人角色',parts['roles']),('工作范围',parts['scope']),('修饰语',parts['modifiers']),('量化指标',parts['metrics'])]:
        for value in values:
            element=key+'='+value
            if element not in elements: elements.append(element)
    return claim.model_copy(update={'elements':elements})


def filter_supports(claim, evidence) -> list[str]:
    """强主张必须有本人归属的明确原文；数量、仓库存在不能升级为强角色。"""
    if evidence.github_record is not None:
        # API 记录的语义由独立的 Python 规则审核，不采用 LLM 的 supports。
        from .github_audit import supported_elements
        return supported_elements(claim,evidence.github_record)
    parts=scope_parts(claim)
    if not parts: return [e for e in evidence.supports if e in claim.elements]
    name=str((claim.entities or {}).get('candidate_name') or '')
    out=[]
    for element in evidence.supports:
        if element not in claim.elements: continue
        field,_,value=element.partition('=')
        strong=field in {'个人角色','工作范围','修饰语','量化指标'} or (field in {'角色','职务'} and (ROLES.search(value) or MODIFIERS.search(value)))
        if strong:
            # 不从项目宣传文字、别人所在行或模糊量化数值推断个人工作。
            if not name: continue
            clauses=re.split(r'[。；;\n，,]',evidence.snippet)
            attributable=[]
            for clause in clauses:
                if name not in clause or value not in clause: continue
                start=clause.find(name)+len(name);end=clause.find(value,start)
                if end<start or end-start>80: continue
                middle=clause[start:end]
                if field in {'修饰语','个人角色','角色','职务'} and re.search(r'协助|辅助|导师|同事|团队成员|参与者|跟随|他人|由别人|不是|并非|没有|不曾|未曾|\bnot\b',middle,re.I): continue
                attributable.append(clause)
            if not attributable: continue
            if urlsplit(evidence.url).hostname in {'github.com','api.github.com'}: continue
        out.append(element)
    return out


def summarize(claim, proved, evidence) -> dict | None:
    parts=scope_parts(claim)
    if not parts: return None
    elements=list(claim.elements)
    rows=[]
    for field,values in [('个人角色',parts['roles']),('工作范围',parts['scope']),('修饰语',parts['modifiers']),('量化指标',parts['metrics'])]:
        for value in values:
            element=field+'='+value
            conflicts=[d for e in evidence for d in e.contradicts if element in d]
            rows.append({'element':element,'original':value,'state':'存在具体冲突' if conflicts else '已支持' if element in proved else '尚无充分证据',
                         'urls':[e.url for e in evidence if element in e.supports or any(element in d for d in e.contradicts)]})
    return {**parts,'checks':rows,'note':'项目存在、本人参与、角色与技术深度分别核对；缺少公开证据不表示经历不实。'}
