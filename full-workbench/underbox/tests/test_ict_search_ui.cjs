const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../index.html'),'utf8');
class Node{constructor(text=''){this.text=String(text);this.children=[];this.handlers={};}addEventListener(k,f){this.handlers[k]=f;}appendChild(n){this.children.push(n);return n;}get textContent(){return this.text+this.children.map(n=>n.textContent).join('');}set textContent(v){this.text=String(v);this.children=[];}set innerHTML(v){if(v===''){this.text='';this.children=[];}else throw Error('untrusted markup');}}
const ctx={URL,window:{},STATUS:{none:{label:'未找到公开记录'},part:{label:'部分证实'},incomplete:{label:'查询遇到问题'},unsearched:{label:'待补充检索信息'}},contextLink:(url,label)=>/^https?:\/\//.test(url)?new Node(label):null,el:(tag,props={},...children)=>{const n=new Node(props.text||'');children.flat().filter(Boolean).forEach(c=>n.appendChild(c));return n;}};
vm.createContext(ctx);vm.runInContext(html.slice(html.indexOf('function retrievalProblems('),html.indexOf('function detailOf(')),ctx);
assert.equal(ctx.retrievalLabel({status:'none',retrievalStatus:'not_started'}),'待补充检索信息');
assert.equal(ctx.retrievalLabel({status:'none'}),'未找到公开记录');
assert.equal(ctx.retrievalLabel({status:'none',retrievalStatus:'budget_not_started'}),'当时金额预算不足');
assert.equal(ctx.retrievalLabel({status:'part',retrievalStatus:'budget_limited'}),'部分证实');
assert.match(ctx.retrievalEmpty({retrievalStatus:'budget_not_started'}),/当时金额预算不足/);
assert.match(ctx.retrievalEmpty({retrievalStatus:'time_limited'}),/当时执行时限触发停止/);
const buckets=new Map();Object.assign(ctx,{REPORT:{claims:[{status:'part',retrievalStatus:'budget_limited'},{status:'none',retrievalStatus:'budget_limited'},{status:'none',retrievalStatus:'budget_not_started'},{status:'none'}]},ORDER:['part','none','incomplete','unsearched'],$:id=>{if(!buckets.has(id))buckets.set(id,new Node());return buckets.get(id);}});
vm.runInContext(html.slice(html.indexOf('function buildFilters()'),html.indexOf('function initFilters()')),ctx);ctx.buildFilters();
assert.match(buckets.get('filters').textContent,/部分证实 1/);assert.match(buckets.get('filters').textContent,/查询遇到问题 2/);assert.match(buckets.get('filters').textContent,/未找到公开记录 1/);
assert.equal(ctx.retrievalDetails({}),null);
const c={verificationAgent:{sourceAccess:{queryPlan:{state:'ready',queryCount:2},decisionCounts:{'正文缺少本人锚点':3},decisions:[{reason:'正文缺少本人锚点',reference:'https://example.test/<script>alert(1)</script>'}],decisionsTruncated:true}}};
const box=ctx.retrievalDetails(c);assert.match(box.textContent,/已生成 2 条查询/);assert.match(box.textContent,/正文缺少本人锚点：3/);assert.match(box.textContent,/逐页记录已达显示上限/);
c.verificationAgent.sourceAccess.queryPlan.state='no_queries';assert.match(ctx.retrievalDetails(c).textContent,/未生成可用查询/);
for(const status of ['partial','provider_error','blocked','timeout','budget_exhausted','canceled']){assert.equal(ctx.displayStatus({status:'none',retrievalStatus:status}),'incomplete');assert.notEqual(ctx.retrievalLabel({status:'none',retrievalStatus:status}),'未找到公开记录');}
// Historical reports derive concrete causes without changing evidence conclusions.
const failure=(reason,reference='https://source.example/record',action='read')=>({action,state:'failed',error:'ReadUnavailable',reason,reference});
const make=events=>({status:'none',retrievalStatus:'partial',verificationAgent:{sourceAccess:{events,queryPlan:{state:'ready',queryCount:2}}}});
for(const [reason,title] of [['HTTP 403','来源拒绝访问'],['HTTP 404','来源链接不可用'],['HTTP 429','来源请求被限流'],['读取超时（>12 秒）','来源读取超时'],['连接超时（>5 秒）','来源连接超时'],['证书校验失败（SSL）','来源证书校验失败'],['旧版 DOC/XLS 未读到文本（读取器缺失、超限或文件受保护）','旧版 Office 附件解析失败'],['PDF 未提取到文本，可能是扫描件或受保护文件','PDF 未提取到文本'],['请求失败（UnsafeURL）','来源地址安全检查失败']]){
 const claim=make([failure(reason)]);assert.equal(ctx.retrievalLabel(claim),title);assert.match(ctx.retrievalEmpty(claim),new RegExp(title));
 const detail=ctx.retrievalDetails(claim).textContent;assert.ok(detail.includes(reason));assert.match(detail,/https:\/\/source.example\/record/);assert.doesNotMatch(detail,/检索未完成/);
}
const mixed=make([failure('HTTP 403'),failure('HTTP 403'),failure('读取超时（>12 秒）')]);
assert.equal(ctx.retrievalProblems(mixed)[0].count,2);assert.match(ctx.retrievalLabel(mixed),/来源拒绝访问等2类/);
mixed.status='part';assert.equal(ctx.retrievalLabel(mixed),'部分证实');assert.equal(ctx.displayStatus(mixed),'part');assert.match(ctx.retrievalProblemText(mixed),/来源读取超时/);
const unknown=make([]);assert.equal(ctx.retrievalLabel(unknown),'查询异常，原因未记录');assert.doesNotMatch(ctx.retrievalDetails(unknown).textContent,/连接超时|HTTP 403/);
const retained=make([]);retained.verificationAgent.sourceAccess={failureGroups:[{action:'read',error:'ReadUnavailable',reason:'HTTP 403',count:150,references:['https://source.example/a']}],counts:{read:{failed:152}},truncated:true};
const issues=ctx.retrievalProblems(retained);assert.equal(issues[0].count,150);assert.equal(issues.find(i=>i.key==='read_error').count,2);assert.match(ctx.retrievalDetails(retained).textContent,/明细有保存上限/);
const complete={status:'none',retrievalStatus:'complete',verificationAgent:{sourceAccess:{decisionCounts:{'正文缺少本人或团队身份锚点':6}}}};
assert.equal(ctx.retrievalLabel(complete),'未找到本人关联证据');assert.match(ctx.retrievalEmpty(complete),/缺少本人或团队身份关联/);
const snapshot=JSON.stringify(mixed);ctx.retrievalProblems(mixed);ctx.retrievalDetails(mixed);assert.equal(JSON.stringify(mixed),snapshot,'rendering history must not rewrite stored evidence or failure records');
const bad=make([failure('<img onerror=alert(1)>','javascript:alert(1)')]);assert.match(ctx.retrievalDetails(bad).textContent,/未保存可打开的来源链接/);
const clue=make([failure('HTTP 403','https://school.example/roster?year=2025'),failure('请求失败（UnsafeURL）','https://school.example/unsafe'),failure('HTTP 404','https://school.example/dead')]);
clue.evidence=[{url:'https://school.example/image',provenanceState:'ocr_unverified',title:'海报名单'},
               {url:'https://school.example/proved',provenanceState:'page_text_verified'}];
Object.assign(clue.verificationAgent.sourceAccess,{
 decisions:[{state:'rejected',reason:'本人关联不足：摘录不含候选人的完整姓名',reference:'https://school.example/notice'},
            {state:'rejected',reason:'搜索结果赛事或赛级不匹配',reference:'https://school.example/mismatch'}],
 searchSamples:[{hits:[
  {url:'https://school.example/roster?utm_medium=search&year=2025',title:'结果名单'},
  {url:'https://school.example/other',title:'候选公告 <script>'},
  ...['proved','mismatch','unsafe','dead'].map(x=>({url:'https://school.example/'+x})),
  {url:'https://127.0.0.1:8000/hidden'},{url:'https://user:pwd@school.example/private'},
  {url:'https://school.example/doc?key=%5Bredacted%5D'},{url:'javascript:alert(1)'}
 ]}]
});
const savedClue=JSON.stringify(clue), links=ctx.manualReviewLinks(clue);
assert.deepEqual(Array.from(links, x=>new URL(x.href).pathname),['/image','/notice','/roster','/other']);
assert.equal(links[2].title,'结果名单');
assert.equal(links[2].reason,'HTTP 403');
assert.match(ctx.renderManualReviewLinks(clue).textContent,/待人工核验的网址 · 4 个/);
assert.match(ctx.renderManualReviewLinks(clue).textContent,/查看其余 1 个网址/);
assert.match(ctx.renderManualReviewLinks(clue).textContent,/候选公告 <script>/);
assert.equal(JSON.stringify(clue),savedClue,'rendering possible links must not rewrite saved evidence');
assert.equal(ctx.renderManualReviewLinks({status:'none'}),null);
assert.ok(!html.includes('检索未完成'),'UI copy must not reintroduce the ambiguous label');
console.log('ICT search state, rejection counts and safe text rendering passed');
const controls=new Map();const byId=id=>{if(!controls.has(id))controls.set(id,{style:{},append(){}});return controls.get(id);};
let requests=0,rendered=null;
Object.assign(ctx,{QUERY_EPOCH:1,PICK:{token:'synthetic-split',claims:Array.from({length:14},(_,i)=>({id:'c'+i})),selected:new Set(Array.from({length:14},(_,i)=>'c'+i))},window:{},document:{body:{dataset:{picking:true}}},$:byId,bringIntoView(){},alert(){throw Error('item-count cap must not interrupt submit');},showPick(){throw Error('unexpected verification failure');},Steps:{set(){},all:()=>Array.from({length:5},()=>({querySelector:()=>({})}))},StatusDock:{resume(){},progress(){},open(){},done(){},fail(message){throw Error(message);}},applyReport(report){rendered=report;},async sseRequest(url,init,callback){requests++;const body=JSON.parse(init.body);assert.equal(body.claimIds.length,14);assert.deepEqual(body.claimIds,Array.from({length:14},(_,i)=>'c'+i));callback({done:true,report:{claims:body.claimIds},seconds:.1});}});
vm.runInContext(html.slice(html.indexOf('function showVerificationResult('),html.indexOf('/* 只有风险清单',html.indexOf('async function runVerify()'))),ctx);
(async()=>{
 await ctx.runVerify();assert.equal(requests,1);assert.equal(rendered.claims.length,14);
 ctx.PICK.selected.clear();await ctx.runVerify();assert.equal(requests,1);
 const task={id:'wb_synthetic',state:'completed',claims:1,createdAt:1};
 ctx.PICK.selected.add('c0');
 const paths=[];let pending;
 ctx.StatusDock.pending=message=>{pending=message;};
 ctx.sseRequest=async(url,init,onEvent)=>{
  paths.push(url);
  if(paths.length===1)onEvent({done:true,failed:true,error:'回执暂未确认',recovery:task});
  else{assert.deepEqual(JSON.parse(init.body),{taskId:task.id});onEvent({done:true,report:{claims:['c0']},seconds:1});}
 };
 await ctx.runVerify();assert.equal(pending,'回执暂未确认');assert.equal(byId('pending').hidden,true);
 assert.equal(byId('resultRecovery').hidden,false);assert.equal(byId('recoveryGo').textContent,'取回本轮结果');
 await ctx.recoverTaskResult();assert.deepEqual(paths,['__ICT_BASE__api/verify/stream','__ICT_BASE__api/ict/recovery']);
 assert.equal(byId('resultRecovery').hidden,true);assert.equal(rendered.claims.length,1);
 console.log('ICT verification selection and receipt-only recovery passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
