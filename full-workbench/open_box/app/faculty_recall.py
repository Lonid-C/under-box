"""教师/创作者按就读、著作、活动和角色取证，不以一条泛授课查询代替。"""
from __future__ import annotations
import re

from .schema import Claim
from .plan import _load_schools, school_domain


def _own_part(part,name):
    if name and re.match(r'\s*'+re.escape(name)+r'(?:教授|老师)?的(?:导师|学生|同事|父母)',part):return False
    if not name:return not re.match(r'\s*(?:其导师|导师|指导老师|同事|参考|推荐|使用)',part)
    return bool(re.match(r'\s*(?:'+re.escape(name)+r'|他|她|本人|主要著作|出版物|著作|教材|主持编写|编写|出版)',part))


def authored_works(text,candidate_name=''):
    """只从有明示编写/出版关系的原文段落取书名，课程/演出引用不当著作。"""
    result=[]
    for part in re.split(r'[。；;\n]', text or ''):
        if not _own_part(part,candidate_name):continue
        titles=re.findall(r'《([^》\n]{2,100})》',part)
        if not titles or not re.search(r'主持编写|编写|编著|著作|主编|出版|ISBN|\[M\]',part,re.I):
            continue
        if re.search(r'参考文献|参考书|选用教材|使用教材|阅读书目|推荐阅读|演奏|首演|期刊论文|发表论文|DOI|\[J\]',part,re.I):
            continue
        role=next((v for v in ['主持编写','执行主编','共同主编','主编','编著','编写','出版'] if v in part),'')
        publishers=[re.sub(r'^(?:分别在|分别由|由|在|和|及)','',p) for p in re.findall(r'[\u4e00-\u9fff]{1,20}?出版社',part)]
        for title in titles:
            result.append({'title':title,'raw':part.strip(),'role':role,'publishers':publishers})
    return list({v['title']:v for v in result}.values())


def enrich_input(claims,text,candidate_name=''):
    """补上模型漏掉的具体书名和毕业院校；所有新增文字仍逐字来自输入。"""
    result=list(claims);ids={c.id for c in claims}
    def ident(prefix):
        n=1
        while f'{prefix}{n}' in ids:n+=1
        value=f'{prefix}{n}';ids.add(value);return value
    works=authored_works(text,candidate_name)
    # 去掉合并的书名列表，只保留原文数量主张，不用几本已找到的书证明总量。
    for i,c in enumerate(result):
        titles=re.findall(r'《([^》]+)》',c.raw_text)
        if len(titles)>1 and not (c.category=='著作' and c.entities.get('title') in titles) and any(w['raw'] in c.raw_text or c.raw_text in w['raw'] for w in works):
            count=re.search(r'(?:共计|总计|合计)\s*(\d+)\s*(部册|部|册|本)',c.raw_text)
            if count:
                result[i]=c.model_copy(update={'category':'著作','elements':['出版数量='+count.group(1)+count.group(2)],
                                              'entities':{**{k:v for k,v in c.entities.items() if k not in {'title','isbn','publisher'}},'publication_total':count.group(1)+count.group(2)}})
            else:
                result[i]=None
    result=[c for c in result if c is not None]
    for w in works:
        existing=next((c for c in result if c.category=='著作' and c.entities.get('title')==w['title']),None)
        elements=['书名='+w['title']]+(['角色='+w['role']] if w['role'] else [])
        # “分别在多家出版社出版”不能给每本书强塞同一出版社。
        if len(set(w['publishers']))==1:elements.append('出版社='+w['publishers'][0])
        item=Claim(id=existing.id if existing else ident('book'),category='著作',raw_text=w['raw'],raw_locator='原文著作列表',date_label='',
                   elements=elements,entities={'title':w['title'],'role':w['role'],'publisher_hints':w['publishers']})
        if existing:result[result.index(existing)]=item
        else:result.append(item)
    for raw in dict.fromkeys(w['raw'] for w in works):
        count=re.search(r'(?:共计|总计|合计)\s*(\d+)\s*(部册|部|册|本)',raw)
        if count and not any(c.entities.get('publication_total')==count.group(1)+count.group(2) for c in result):
            value=count.group(1)+count.group(2)
            result.append(Claim(id=ident('booktotal'),category='著作',raw_text=raw,raw_locator='原文出版总量',date_label='',
                                elements=['出版数量='+value],entities={'publication_total':value}))
    for m in re.finditer(r'毕业于([^，,。；;\n]{2,100})',text or ''):
        part=re.split(r'[。；;\n]',text[:m.start()])[-1]+m.group()
        if not _own_part(part,candidate_name):continue
        prefix=re.search(r'(?:(?:19|20)\d{2}\s*年?\s*)?(?:本科(?:生)?|硕士(?:研究生)?|博士(?:研究生)?)?\s*$',text[max(0,m.start()-20):m.start()])
        raw=(prefix.group() if prefix else '')+m.group();tail=m.group(1)
        schools=[s['name'] for s in _load_schools() if tail.startswith(s['name'])]
        if not schools:continue
        school=max(schools,key=len)
        from .academic import stages_in,study_stage
        stages=stages_in(raw)
        if any(c.category=='学历' and school in c.raw_text and (not stages or study_stage(c) in stages) for c in result):continue
        field=re.split(r'专业|并|后|从|留校',tail[len(school):],maxsplit=1)[0].strip()
        elements=['学校='+school]
        if field and len(field)<35:elements.append('专业='+field)
        if len(stages)==1:elements.append('学段='+next(iter(stages)))
        year=re.search(r'(?:19|20)\d{2}',raw)
        result.append(Claim(id=ident('edu'),category='学历',raw_text=raw,raw_locator='原文毕业经历',date_label=year.group() if year else '',
                            elements=elements,entities={'org':school,'major':field if field else ''}))
    return result


def queries_for(claim,name,profile):
    """短查询先找具体事实；年代扩展留在后面，不把学位猜成保研/直博。"""
    from .strategy import Query,_name_variants
    from .plan import claim_school
    from .academic import study_stage
    names=_name_variants(name)
    if not names:return []
    q=[];raw=claim.raw_text or '';ent=claim.entities or {};context=ent.get('candidate_identity_context') or {}
    def add(text,purpose,site=None,weight=145):
        q.append(Query(text=text,site=site,round=1,weight=weight,source='faculty_fact:'+claim.category,purpose=purpose,confirm_empty=bool(site)))
    who='"'+names[0]+'"'
    if claim.category=='著作':
        title=ent.get('title') or next((e.partition('=')[2] for e in claim.elements if e.startswith('书名=')),'')
        if title and title in raw:
            add(f'{who} "{title}"','书名与作者交叉，先查出版记录')
            add(f'{who} "{title}" 出版社 ISBN','查出版社书目与版本身份',weight=144)
            add(f'{who} "{title}" 图书馆 编者','查馆藏责任者；不能把推荐书目当本人著作',weight=143)
            add(f'{who} "{title}" 书目 filetype:pdf','查可读取的馆藏/出版社书目原文，跳过只有脚本的目录页',weight=142)
        else:
            add(f'{who} 教材 出版 数量','核对总量原始记录，几本书不能证明总量')
    elif claim.category=='学历' and not study_stage(claim) and (getattr(profile,'identity','') in {'researcher','creator'} or context.get('current_employers')):
        school=claim_school(claim)
        if school:
            for word in ['毕业','作者简介','校友']:
                add(f'{who} "{school}" {word}','先查明示毕业事实与本人履历，不猜学位或入学方式',weight=145-len(q))
    elif claim.category in {'教学活动','会议','艺术成果'}:
        topics=re.findall(r'《([^》]+)》|[“"]([^”"]{6,100})[”"]',raw)
        for values in topics[:2]:
            title=next((v for v in values if v),'')
            if title:add(f'{who} "{title}"','先查原文活动/作品名与本人记录')
        field=next((r.get('field') for r in context.get('education_fields',[]) if r.get('field')),'')
        if not field:
            field=next((v for v in ['视唱练耳','乐理','钢琴','计算机','音乐'] if v in raw),'')
        # 原始目录常只写学科词；“专业/方向”是字段说明，不应成为检索硬条件。
        field=re.sub(r'(?:专业|方向|学科)$','',field).strip()
        if re.search(r'研究生导师|硕士生导师|博士生导师',raw):
            add(f'{who} {field} 导师','核对导师角色；学生的硕士/博士不代表老师的学历')
            for school in context.get('education_schools',[])[:1]:
                add(f'{who} {field} 答辩','查原就读/工作学校的导师及答辩记录',school_domain(school),144)
        if re.search(r'视频|公开课|授课|课程',raw):
            add(f'{who} {field} 教学视频' if '视频' in raw else f'{who} {field} 授课 课程','按本条教学目的查课程原始记录',weight=144)
        if re.search(r'出版|教程|教材',raw):
            add(f'{who} {field} 教材 出版','教材出版与课程教学分别取证',weight=143)
        if re.search(r'论坛|研讨会|艺术周|学术活动',raw):
            add(f'{who} {field} 论坛 议程','查举办方议程与个人角色',weight=144)
            add(f'{who} {field} 研讨会 主持','核对主持/策划角色，活动存在不证明全部角色',weight=143)
    return q


def book_supports(claim,evidence):
    """题名、本人角色、独立性、数量分开；角色不能从一张书目推断。"""
    if claim.category!='著作':return evidence.supports
    from .person_scope import _name_patterns
    name=(claim.entities or {}).get('candidate_name') or ((claim.entities or {}).get('candidate_identity_context') or {}).get('candidate_name','')
    patterns=_name_patterns(name) if name else []
    result=[];body=evidence.snippet
    for element in evidence.supports:
        key,_,value=element.partition('=')
        if key in {'角色','主编','编者','作者位次','独立性','出版数量','教材总数','卷册信息'}:
            if value not in body:continue
            if key!='卷册信息' and not any(value in part and any(p.search(part) for p in patterns) for part in re.split(r'[。；;\n]',body)):continue
            if value=='主编' and re.search(r'执行主编|共同主编|副主编',body) and not re.search(r'(?<!执行)(?<!共同)(?<!副)主编',body):continue
            if re.search(r'独立|独著|唯一',value) and re.search(r'共同|合著|合编|联合',body):continue
        result.append(element)
    return result


def book_contradicts(claim,evidence):
    if claim.category!='著作':return evidence.contradicts
    # 编著/共同署名没有直接否定“主持编写”，没有列数量也不是数量矛盾。
    return [v for v in evidence.contradicts if not re.search(r'角色|主编|编写|独立|独著|数量|总数|主持',v)
            or re.search(r'没有|并非|不是|不曾|未曾|仅负责|只负责',evidence.snippet)]
