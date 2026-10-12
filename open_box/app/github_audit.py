"""只读与本条项目明确关联的公开 GitHub 链接；不找账号、不遍历其他仓库。

API 记录和 diff 是材料；来源等级、证明范围、冲突和最终状态均由 Python 规则决定。
"""
from __future__ import annotations
import json,re,time
from urllib.parse import urlsplit,quote
import httpx
from .schema import Evidence

URL_RE=re.compile(r'https?://(?:www\.)?github\.com/[^\s<>"\u3000，。；（）)\],;]+',re.I)
ACCOUNT_RE=re.compile(r'(?im)^\s*(?:(?:我的|本人|个人)\s*)?GitHub\s*(?:账号|账户|个人主页|主页)?\s*[:：]\s*(https?://(?:www\.)?github\.com/[A-Za-z0-9-]+/?)\s*(?:$|[，,；;])')
LIMIT_REQUESTS=20


def parse_link(url):
    try:
        p=urlsplit(str(url).strip().rstrip('.,;，。；)）]】'));port=p.port
    except ValueError: return None
    if p.scheme not in {'http','https'} or p.hostname not in {'github.com','www.github.com'} or p.username or p.password or port not in {None,80,443}: return None
    parts=p.path.strip('/').split('/')
    if not parts or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})',parts[0]): return None
    if len(parts)==1: return {'kind':'account','account':parts[0],'url':'https://github.com/'+parts[0]}
    repo=parts[1].removesuffix('.git')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',repo) or repo in {'.','..'}: return None
    base=parts[0]+'/'+repo; kind='repo'; ident=''
    if len(parts)>2:
        if len(parts) in {4,5} and parts[2]=='pull' and parts[3].isdigit() and (len(parts)==4 or parts[4] in {'files','commits'}): kind='pr';ident=parts[3]
        elif len(parts)==4 and parts[2]=='commit' and re.fullmatch(r'[0-9a-fA-F]{7,40}',parts[3]): kind='commit';ident=parts[3]
        else: return None
    return {'kind':kind,'repo':base,'id':ident,'url':'https://github.com/'+base+(('/pull/' if kind=='pr' else '/commit/')+ident if ident else '')}


def links_in(text):
    return [p for m in URL_RE.finditer(text or '') if (p:=parse_link(m.group()))]


def _mentions_repository(text):
    if any(p['kind'] != 'account' for p in links_in(text)):
        return True
    # 单纯列账号、熟悉 GitHub 或使用 Git 不表示该项目有仓库。
    text = re.sub(r'(?:熟悉|了解|掌握|会使用)\s*GitHub[^。；;\n，,]*', '', text or '', flags=re.I)
    return bool(re.search(r'(?:GitHub[^。；;\n]{0,45}(?:仓库|项目源码|源码|repository|repo\b)|'
                          r'(?:仓库|项目源码|源码|repository|repo\b)[^。；;\n]{0,45}GitHub)', text or '', re.I)
                and not re.search(r'(?:没有|无|未提供|不提供|并无)\s*(?:GitHub\s*)?仓库', text or '', re.I))


def material_eligible(claim):
    if claim.category not in {'项目', '开源项目'}:
        return False
    ent = claim.entities or {}
    if ent.get('github_input_checked'):
        return ent.get('github_material_eligible') is True
    return _mentions_repository(claim.raw_text)


def bind_input(claims, text):
    """校验模型关联的 URL 确实出现在原始输入的同一项目段落，不传播个人主页。"""
    accounts=list(dict.fromkeys(p['account'] for m in ACCOUNT_RE.finditer(text or '') if (p:=parse_link(m.group(1))) and p['kind']=='account'))
    blocks=re.split(r'\n\s*\n',text or '')
    out=[]
    for claim in claims:
        if claim.category not in {'项目','开源项目'}: out.append(claim);continue
        ent=dict(claim.entities or {});bound=[];eligible=False
        compact=lambda s:re.sub(r'\s+','',s)
        raw=compact(claim.raw_text)
        original=compact(text or '')
        if raw and raw in original:
            eligible = _mentions_repository(claim.raw_text)
            bound=[p['url'] for p in links_in(claim.raw_text) if p['kind']!='account']
            for block in blocks:
                if raw not in compact(block): continue
                matches=[c for c in claims if c.category in {'项目','开源项目'} and compact(c.raw_text) and compact(c.raw_text) in compact(block)]
                # 多个项目共用一段时只接受直接出现在本人陈述内的链接。
                if len(matches)==1:
                    bound.extend(p['url'] for p in links_in(block) if p['kind']!='account')
                    eligible |= _mentions_repository(block)
        ent.update(github_input_checked=True,github_bound_urls=list(dict.fromkeys(bound)),github_accounts=accounts,
                   github_material_eligible=bool(eligible or bound))
        # 不接受模型根据仓库 owner 猜出的本人账号。
        ent.pop('github',None)
        out.append(claim.model_copy(update={'entities':ent}))
    return out


class AuditEvidence(list):
    def __init__(self, values, audit): super().__init__(values);self.github_audit=audit


class Unavailable(Exception): pass


class GitHubClient:
    def __init__(self, transport=None, max_requests=LIMIT_REQUESTS, max_seconds=75):
        self.calls=0;self.max_requests=max_requests;self.deadline=time.monotonic()+max_seconds
        self.warnings=[];self.truncated=False;self.blocked=False
        self.client=httpx.Client(transport=transport,timeout=10,follow_redirects=False,headers={'Accept':'application/vnd.github+json','User-Agent':'Underbox-public-claim-verifier','X-GitHub-Api-Version':'2022-11-28'})
    def close(self): self.client.close()
    def get(self,path,params=None):
        if self.blocked: raise Unavailable('GitHub 已限流或拒绝访问，本次不继续重试')
        if not path.startswith('/') or '..' in path.split('/') or '://' in path: raise Unavailable('无效 API 范围')
        if self.calls>=self.max_requests or time.monotonic()>self.deadline:
            self.truncated=True;raise Unavailable('已达请求/时间预算，未检查的部分保持未知')
        self.calls+=1
        try: r=self.client.get('https://api.github.com'+path,params=params,timeout=min(10,max(.1,self.deadline-time.monotonic())))
        except httpx.HTTPError: raise Unavailable('GitHub 网络或超时，不能据此否定经历')
        if r.status_code in {403,429}:
            self.blocked=True
            raise Unavailable('GitHub 限流或拒绝访问，请稍后重试；不等于没有贡献')
        if r.status_code in {404,410}: raise Unavailable('公开记录不可访问（可能私有、删除或地址变化）')
        if r.status_code!=200: raise Unavailable(f'GitHub HTTP {r.status_code}，本次无法核验')
        if len(r.content)>2_000_000: self.truncated=True;raise Unavailable('返回内容超过读取上限')
        try: data=r.json()
        except ValueError: raise Unavailable('GitHub 返回格式不可读')
        return data,r.links.get('next') is not None
    def pages(self,path,params=None,max_pages=3):
        out=[]
        for page in range(1,max_pages+1):
            data,nxt=self.get(path,{**(params or {}),'per_page':100,'page':page})
            values=data.get('files',[]) if isinstance(data,dict) else data
            if not isinstance(values,list): raise Unavailable('GitHub 列表格式不符')
            out.extend(values)
            if not nxt: return out,False
        self.truncated=True
        return out,True


def file_kind(path):
    p=path.lower()
    if p.startswith(('docs/','doc/')) or p.endswith(('.md','.rst','.txt')): return '文档'
    if re.search(r'(^|/)(tests?|__tests__)(/|$)|[._](test|spec)\.',p): return '测试'
    if p.startswith(('.github/','ci/')) or p.endswith(('.yaml','.yml','.toml')) or p.rsplit('/',1)[-1] in {'dockerfile','makefile','package.json','tsconfig.json','cargo.lock','requirements.txt'}: return '配置/构建'
    if p.endswith(('.py','.js','.ts','.tsx','.jsx','.go','.rs','.java','.c','.cpp','.h','.cc','.cs','.rb','.vue')): return '代码（功能/修复/架构需进一步核对）'
    return '其他/未确认'


def domain_hint(path):
    """只据路径标明的范围，不把语言或仓库领域赋给候选人。"""
    p='/'+path.lower()
    if file_kind(path)=='文档': return '文档/说明材料'
    if file_kind(path)=='配置/构建': return '构建或配置路径'
    for pattern,label in [(r'/(frontend|components|ui|views)/','界面相关命名路径'),
                          (r'/(runtime|scheduler|inference)/','运行时/调度/推理命名路径'),
                          (r'/(training|train)/','训练相关命名路径'),
                          (r'/(storage|database|db)/','存储/数据库命名路径'),
                          (r'/(server|backend|api)/','服务/接口命名路径')]:
        if re.search(pattern,p): return label+'（具体职责须结合 diff 复核）'
    return '仅凭路径不能确认技术领域'


def diff_analysis(llm, files):
    """模型只描述真实 hunk；模型声称的主导/能力/分数/证明范围均丢弃。"""
    if not llm or not any(f.get('patch') for f in files): return None
    from .parse import wrap_untrusted
    data=[{'path':f['path'],'patch':f.get('patch','')[:2000]} for f in files[:8]]
    try:
        got=llm.complete_json('返回 JSON {observations:[{path,quote,description,change_type}]}。change_type 仅可为 文档/配置/测试/修复/功能/架构/未确认；属于内容解读而非最终证据判断。只描述 diff 中实际改动与可能技术领域，不评价能力、不判断个人角色、不输出评分或 supports；quote 须逐字取自该文件 patch。',wrap_untrusted(json.dumps(data,ensure_ascii=False)))
    except Exception: return None
    observations=[]
    for row in (got.get('observations',[]) if isinstance(got,dict) else []):
        if not isinstance(row,dict): continue
        f=next((f for f in data if f['path']==row.get('path')),None)
        q=row.get('quote');desc=row.get('description')
        if f and isinstance(q,str) and 0<len(q)<=300 and q in f['patch'] and isinstance(desc,str) and not re.search(r'造假|水货|虚假|能力|面试|录用|评分|排名|主导|核心贡献者|独立开发|自研',desc):
            observations.append({'path':f['path'],'quote':q,'description':desc[:300],'suggestedType':row.get('change_type') if row.get('change_type') in {'文档','配置','测试','修复','功能','架构','未确认'} else '未确认'})
    return {'observations':observations,'note':'diff 内容分析，仅供复核；不参与最终身份、等级或状态判定'} if observations else None


def tier_for_record(rec):
    """平台元数据仍为 C；可追溯、账号关联且有原始 diff 的贡献记录为 B。"""
    link=parse_link(rec.get('url',''))
    if (link and link.get('kind') in {'pr','commit'} and rec.get('source')=='github_public_api'
            and rec.get('recordExists') and rec.get('actorConfirmed') and rec.get('filesComplete')
            and any(f.get('patchAvailable') and f.get('patch') for f in rec.get('files',[]))): return 'B'
    return 'C'


def supported_elements(claim, rec):
    out=[];actor=rec.get('actorConfirmed',False);kind=rec.get('kind');files=rec.get('files',[])
    for e in claim.elements:
        k,_,v=e.partition('=')
        if k=='关联项目链接' and (p:=parse_link(v)):
            if (p['kind']=='repo' and rec.get('repoExists') and p.get('repo','').lower()==rec.get('repo','').lower()) or (rec.get('recordExists') and p['url'].lower()==rec.get('url','').lower()): out.append(e)
        elif k in {'项目','项目名称','仓库','repo','事实锚点'} and rec.get('repoExists') and re.sub(r'\W','',v).casefold() in {re.sub(r'\W','',rec.get('repo','')).casefold(),re.sub(r'\W','',rec.get('repo','').split('/')[-1]).casefold()}: out.append(e)
        elif actor and kind in {'pr','commit'}:
            if k=='本人参与' or (k in {'个人角色','角色'} and v.casefold() in {'参与','贡献','participated','contributed','contributor','提交pr'}): out.append(e)
            elif k=='工作范围' and (v in {f['path'] for f in files} or v=='文档' and any(f['type']=='文档' for f in files)): out.append(e)
            elif k in {'合并状态','状态'} and v in {'已合并','merged'} and rec.get('merged') is True: out.append(e)
    return out


def record_conflicts(claim, rec):
    out=[];raw=claim.raw_text
    if rec.get('kind')=='pr' and rec.get('explicit') and rec.get('actorConfirmed'):
        if re.search(r'已合并|已合入|已被采纳|\bmerged\b',raw,re.I) and rec.get('merged') is False:
            out.append(f"合并状态：本人明确提供的 PR #{rec['number']} 当前 {rec['state']}，API merged=false；这不是个人能力结论")
        if (re.search(r'该\s*PR|本\s*PR|这次\s*PR',raw,re.I) and re.search(r'核心架构|架构代码',raw)
                and rec.get('filesComplete') and rec.get('files') and all(f['type']=='文档' for f in rec['files'])):
            out.append('修饰语=核心架构：该 PR 的完整文件列表仅涉及文档，不能把本次文档改动表述为架构代码改动；不推断其他贡献')
    if rec.get('fork') and re.search(r'不是\s*fork|非\s*fork|从未\s*fork',raw,re.I): out.append('仓库类型：GitHub API fork=true，与“非 fork”表述冲突；fork 和二次开发本身是正常实践')
    return out


def audit_claim(claim, links, llm=None, client=None, progress=None, budget=None):
    owned=client is None;client=client or GitHubClient(max_requests=min(LIMIT_REQUESTS,max(0,budget.max_page_reads-budget.page_reads)) if budget else LIMIT_REQUESTS,max_seconds=max(0,budget.max_seconds-budget.elapsed()) if budget else 75);say=progress or (lambda s:None)
    audit={'triggered':True,'scope':[p['url'] for p in links],'identityConfirmed':False,'identityBasis':'候选人须主动标明本人账号；不按姓名或仓库 owner 猜测','repositories':[], 'records':[], 'warnings':[], 'truncated':False,
           'limitations':['只核验本条提供的公开仓库与链接；分页/请求上限之外的记录未检查。','PR 数、commit 数、Stars、行数仅作上下文，不等于主导、核心、独立、自研或个人能力。','协作、squash、私有开发、代码迁移均可能导致公开记录不完整；文档贡献是正常且有价值的贡献。']}
    evidence=[]
    accounts=(claim.entities or {}).get('github_accounts',[])
    accounts=[a for a in accounts if isinstance(a,str) and re.fullmatch(r'[A-Za-z0-9-]{1,39}',a)] if isinstance(accounts,list) else []
    def append_record(rec):
        rec['source']='github_public_api';rec['recordExists']=True
        audit['records'].append(rec)
        values={'repository':rec['repo'],'author':rec.get('actor'),'state':rec.get('state'),'merged':rec.get('merged'),'files':[{'filename':f['path'],'patch':f.get('patch','')[:600]} for f in rec.get('files',[])[:8]]}
        evidence.append(Evidence(url=rec['url'],title=rec.get('title') or 'GitHub API 公开记录',publisher='GitHub',source_tier=tier_for_record(rec),snippet=json.dumps(values,ensure_ascii=False),identity_signals=['账号由候选人提供','候选人自提供该链接'],supports=supported_elements(claim,rec),contradicts=record_conflicts(claim,rec),github_record=rec))
    def files_for(path):
        raw,truncated=client.pages(path)
        files=[{'path':str(f.get('filename',''))[:300],'type':file_kind(str(f.get('filename',''))),'status':f.get('status'),'additions':f.get('additions',0),'deletions':f.get('deletions',0),'patch':str(f.get('patch') or '')[:4000] if index<12 else '','domainHint':domain_hint(str(f.get('filename',''))),'patchAvailable':isinstance(f.get('patch'),str),'patchTruncated':not isinstance(f.get('patch'),str) or index>=12 or len(str(f.get('patch') or ''))>4000} for index,f in enumerate(raw) if isinstance(f,dict)]
        return files,not truncated
    try:
        if not accounts:
            audit['identityUnknown']=True
            audit['warnings'].append('账号归属无法确认，未读取 PR、commit 或 diff；请候选人主动补充本人 GitHub 账号与项目关联链接。')
            return AuditEvidence([],audit),False
        confirmed=[]
        for account in accounts[:2]:
            user,_=client.get('/users/'+quote(account,safe=''))
            if isinstance(user,dict) and str(user.get('login','')).casefold()==account.casefold() and user.get('type')=='User': confirmed.append(str(user['login']))
        if not confirmed:
            audit['identityUnknown']=True
            raise Unavailable('提供的本人账号未能确认为公开 GitHub 用户，未做贡献分析')
        audit['identityConfirmed']=True;audit['identityBasis']='候选人主动提供本人账号，API 确认账号存在；记录作者须与该账号相同。自然人身份未作独立认证。'
        repos=list(dict.fromkeys(p['repo'] for p in links))[:3]
        if len(set(p['repo'] for p in links))>3: audit['truncated']=True
        for repo in repos:
            say('GitHub：仅读取关联仓库 '+repo)
            try:
                meta,_=client.get('/repos/'+repo)
                if not isinstance(meta,dict) or meta.get('private') is not False: raise Unavailable('仓库不是可确认的公开仓库，已跳过')
                if str(meta.get('full_name','')).casefold()!=repo.casefold(): raise Unavailable('仓库地址已迁移或不对应；需补充确认后的链接')
                own=str((meta.get('owner') or {}).get('login','')).casefold() in {a.casefold() for a in confirmed} and (meta.get('owner') or {}).get('type')=='User'
                audit['repositories'].append({'repo':repo,'url':'https://github.com/'+repo,'kind':'自有仓库' if own else '外部/组织仓库','fork':bool(meta.get('fork')),'stars':meta.get('stargazers_count',0),'language':meta.get('language'),'note':'仓库归属不等于独立开发；fork 不等于非原创或造假'})
                append_record({'kind':'repo','repo':repo,'repoExists':True,'url':'https://github.com/'+repo,'actorConfirmed':False,'fork':bool(meta.get('fork')),'title':str(meta.get('name') or repo)})
                targets=[p for p in links if p['repo']==repo and p['kind']!='repo']
                if not targets:
                    pulls,trunc=client.pages('/repos/'+repo+'/pulls',{'state':'all','sort':'updated','direction':'desc'},max_pages=2)
                    audit['truncated']|=trunc
                    authored=[p for p in pulls if isinstance(p,dict) and isinstance(p.get('number'),int) and str((p.get('user') or {}).get('login','')).casefold() in {a.casefold() for a in confirmed}]
                    if len(authored)>3: audit['truncated']=True
                    targets=[{'kind':'pr','repo':repo,'id':str(p['number']),'url':f"https://github.com/{repo}/pull/{p['number']}",'sampled':True} for p in authored[:3]]
                    for account in confirmed[:1]:
                        commits,trunc=client.pages('/repos/'+repo+'/commits',{'author':account},max_pages=1);audit['truncated']|=trunc
                        targets.extend({'kind':'commit','repo':repo,'id':c['sha'],'url':f"https://github.com/{repo}/commit/{c['sha']}",'sampled':True} for c in commits if str((c.get('author') or {}).get('login','')).casefold()==account.casefold())
                if len(targets)>5: audit['truncated']=True
                for target in targets[:5]:
                    kind=target['kind'];path='/repos/'+repo+('/pulls/' if kind=='pr' else '/commits/')+target['id']
                    obj,_=client.get(path)
                    if not isinstance(obj,dict): raise Unavailable('GitHub 贡献详情格式不符')
                    actor=str(((obj.get('user') if kind=='pr' else obj.get('author')) or {}).get('login',''))
                    if actor.casefold() not in {a.casefold() for a in confirmed}:
                        audit['warnings'].append(target['url']+'：作者未与候选人提供的账号对应，不计入本人证据，未读取 diff');audit['unmatchedAuthor']=True;continue
                    verification=(obj.get('commit') or {}).get('verification') or {}
                    if kind=='commit' and not (verification.get('verified') is True and verification.get('reason')=='valid'):
                        audit['unverifiedCommit']=True
                        audit['warnings'].append(target['url']+'：提交作者关联是平台元数据，签名未确认；不据此单独证明本人提交，未进一步读取或分析 diff。可补充本人 PR 或署名关联材料。')
                        append_record({'kind':'commit','repo':repo,'repoExists':True,'url':target['url'],'actor':actor,'actorConfirmed':False,'signatureVerified':False,'title':str((obj.get('commit') or {}).get('message') or '')[:300],'files':[],'filesComplete':False})
                        continue
                    try: files,complete=files_for(path+'/files' if kind=='pr' else path)
                    except Unavailable as exc:
                        audit['warnings'].append(target['url']+'：'+str(exc));files=[];complete=False
                    if kind=='pr' and isinstance(obj.get('changed_files'),int) and obj['changed_files']>len(files): complete=False;audit['truncated']=True
                    rec={'kind':kind,'repo':repo,'repoExists':True,'url':target['url'],'actor':actor,'actorConfirmed':True,'title':str(obj.get('title') or (obj.get('commit') or {}).get('message') or '')[:300],'number':obj.get('number'),'state':obj.get('state'),'merged':obj.get('merged') if kind=='pr' else None,'explicit':not target.get('sampled',False),'files':files,'filesComplete':complete,'analysis':diff_analysis(llm,files)}
                    append_record(rec)
            except Unavailable as exc: audit['warnings'].append(repo+'：'+str(exc))
    except Unavailable as exc: audit['warnings'].append(str(exc))
    finally:
        audit['requests']=client.calls;audit['truncated']|=client.truncated
        if budget:
            budget.page_reads+=client.calls;budget.exhausted|=client.truncated
        if owned: client.close()
    return AuditEvidence(evidence,audit),client.truncated


def supplement_claims(claims, materials):
    """材料是用户为具体条目主动补充的 URL，不把账号扩散成全仓库调查。"""
    from .claim_scope import enrich_claim
    if not isinstance(materials,dict): raise ValueError('GitHub 补充材料格式错误')
    if set(materials)-{c.id for c in claims}: raise ValueError('补充材料包含不存在的条目')
    out=[]
    for c in claims:
        material=materials.get(c.id)
        if material is None: out.append(c);continue
        if c.category not in {'项目','开源项目'}: raise ValueError('GitHub 材料只能关联到项目条目')
        if not material_eligible(c): raise ValueError('只有原始输入明确提及 GitHub 仓库的项目可以补充 GitHub 材料')
        if not isinstance(material,dict): raise ValueError('GitHub 材料格式错误')
        accounts=material.get('accounts',[]);urls=material.get('urls',[])
        if not isinstance(accounts,list) or not isinstance(urls,list) or len(accounts)>2 or len(urls)>6: raise ValueError('每条最多 2 个本人账号、6 个项目链接')
        parsed_accounts=[parse_link(u) for u in accounts];parsed_urls=[parse_link(u) for u in urls]
        if any(p is None or p['kind']!='account' for p in parsed_accounts): raise ValueError('本人账号须是公开 GitHub 个人主页 URL')
        if any(p is None or p['kind']=='account' for p in parsed_urls): raise ValueError('项目链接须是 GitHub 仓库、PR 或 commit')
        ent=dict(c.entities or {})
        ent['github_accounts']=list(dict.fromkeys([*(ent.get('github_accounts') or []),*(p['account'] for p in parsed_accounts)]))[:2]
        ent['github_bound_urls']=list(dict.fromkeys([*(ent.get('github_bound_urls') or []),*(p['url'] for p in parsed_urls)]))[:6]
        ent['github_input_checked']=True
        out.append(enrich_claim(c.model_copy(update={'entities':ent})))
    return out
