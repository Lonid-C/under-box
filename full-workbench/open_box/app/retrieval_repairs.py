"""Grounded activity and role routes for the isolated ICT edition."""
import re


def exchange_activity(claim):
    return (claim.category in {'校内荣誉','项目','交流活动'}
            and bool(re.search(r'访问交流|访问学习|访学|交换生|交换学习|学术交流',claim.raw_text))
            and not re.search(r'获奖|奖学金|[一二三特]等奖|优秀(?:学员|营员|学生)|获评|荣获',claim.raw_text))


def exchange_plan(claim,name):
    from .strategy import Plan
    from .plan import Query,claim_school,school_domain
    e=claim.entities;org=e.get('org','');school=claim_school(claim)
    year=(claim.date_start or claim.date_label or '')[:4]
    year=year if year.isdigit() else ''
    if org and re.sub(r'\W','',org) not in re.sub(r'\W','',claim.raw_text):org=''
    rows=[]
    if name and org:rows.append(Query(f'"{name}" "{org}" {year}',round=1,purpose='按原文姓名与机构定位交流参与记录'))
    site=school_domain(school) if school else None
    if name:rows.append(Query(f'"{name}" {year} 访问 交流',site=site,round=1,purpose='查学校公开交流参与记录，不当作获奖'))
    if org:rows.append(Query(f'"{org}" {year} 访问 交流',site=site,round=2,purpose='查活动公告，再核本人参与'))
    return Plan(rows,'exchange_activity','访问与交流活动',
                notes=['核对本人参与、机构、活动及时间；活动存在不证明本人参与，不套用评奖查询。'],
                budget={'searches':24,'reads':10,'seconds':120})


def role_variants(claim):
    compact=lambda s:re.sub(r'\W','',s)
    values=[e.partition('=')[2].strip() for e in claim.elements
            if e.partition('=')[0].strip() in {'职务','岗位','职位','角色'}]
    if not values:values=re.split(r'[、，,;/；]+',str(claim.entities.get('role') or ''))
    return list(dict.fromkeys(v for v in values if 2<=len(v)<=40 and compact(v) in compact(claim.raw_text)))[:4]
