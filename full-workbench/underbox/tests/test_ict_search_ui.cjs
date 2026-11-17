const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../index.html'),'utf8');
class Node{constructor(text=''){this.text=String(text);this.children=[];}appendChild(n){this.children.push(n);return n;}get textContent(){return this.text+this.children.map(n=>n.textContent).join('');}set textContent(v){this.text=String(v);this.children=[];}set innerHTML(v){if(v===''){this.text='';this.children=[];}else throw Error('untrusted markup');}}
const ctx={STATUS:{none:{label:'未找到公开记录'},part:{label:'部分证实'},incomplete:{label:'检索未完成'},unsearched:{label:'待补充检索信息'}},el:(tag,props={},...children)=>{const n=new Node(props.text||'');children.forEach(c=>n.appendChild(c));return n;}};
vm.createContext(ctx);vm.runInContext(html.slice(html.indexOf('function retrievalLabel('),html.indexOf('function detailOf(')),ctx);
assert.equal(ctx.retrievalLabel({status:'none',retrievalStatus:'not_started'}),'待补充检索信息');
assert.equal(ctx.retrievalLabel({status:'none'}),'未找到公开记录');
assert.equal(ctx.retrievalLabel({status:'none',retrievalStatus:'budget_not_started'}),'预算不足，未执行');
assert.equal(ctx.retrievalLabel({status:'part',retrievalStatus:'budget_limited'}),'部分证实');
assert.match(ctx.retrievalEmpty({retrievalStatus:'budget_not_started'}),/未执行检索/);
assert.match(ctx.retrievalEmpty({retrievalStatus:'time_limited'}),/检索未完成/);
const buckets=new Map();Object.assign(ctx,{REPORT:{claims:[{status:'part',retrievalStatus:'budget_limited'},{status:'none',retrievalStatus:'budget_limited'},{status:'none',retrievalStatus:'budget_not_started'},{status:'none'}]},ORDER:['part','none','incomplete','unsearched'],$:id=>{if(!buckets.has(id))buckets.set(id,new Node());return buckets.get(id);}});
vm.runInContext(html.slice(html.indexOf('function buildFilters()'),html.indexOf('function initFilters()')),ctx);ctx.buildFilters();
assert.match(buckets.get('filters').textContent,/部分证实 1/);assert.match(buckets.get('filters').textContent,/检索未完成 2/);assert.match(buckets.get('filters').textContent,/未找到公开记录 1/);
assert.equal(ctx.retrievalDetails({}),null);
const c={verificationAgent:{sourceAccess:{queryPlan:{state:'ready',queryCount:2},decisionCounts:{'正文缺少本人锚点':3},decisions:[{reason:'正文缺少本人锚点',reference:'https://example.test/<script>alert(1)</script>'}],decisionsTruncated:true}}};
const box=ctx.retrievalDetails(c);assert.match(box.textContent,/已生成 2 条查询/);assert.match(box.textContent,/正文缺少本人锚点：3/);assert.match(box.textContent,/逐页记录已达显示上限/);
c.verificationAgent.sourceAccess.queryPlan.state='no_queries';assert.match(ctx.retrievalDetails(c).textContent,/未生成可用查询/);
for(const status of ['partial','provider_error','blocked','timeout','budget_exhausted','canceled']){assert.equal(ctx.displayStatus({status:'none',retrievalStatus:status}),'incomplete');assert.notEqual(ctx.retrievalLabel({status:'none',retrievalStatus:status}),'未找到公开记录');}
console.log('ICT search state, rejection counts and safe text rendering passed');
const controls=new Map();const byId=id=>{if(!controls.has(id))controls.set(id,{style:{},append(){}});return controls.get(id);};
let requests=0,rendered=null;
Object.assign(ctx,{QUERY_EPOCH:1,PICK:{token:'synthetic-split',claims:Array.from({length:14},(_,i)=>({id:'c'+i})),selected:new Set(Array.from({length:14},(_,i)=>'c'+i))},window:{},document:{body:{dataset:{picking:true}}},$:byId,bringIntoView(){},alert(){throw Error('item-count cap must not interrupt submit');},showPick(){throw Error('unexpected verification failure');},Steps:{set(){},all:()=>Array.from({length:5},()=>({querySelector:()=>({})}))},StatusDock:{resume(){},progress(){},open(){},done(){},fail(message){throw Error(message);}},applyReport(report){rendered=report;},async sseRequest(url,init,callback){requests++;const body=JSON.parse(init.body);assert.equal(body.claimIds.length,14);assert.deepEqual(body.claimIds,Array.from({length:14},(_,i)=>'c'+i));callback({done:true,report:{claims:body.claimIds},seconds:.1});}});
vm.runInContext(html.slice(html.indexOf('async function runVerify()'),html.indexOf('/* 只有风险清单',html.indexOf('async function runVerify()'))),ctx);
(async()=>{await ctx.runVerify();assert.equal(requests,1);assert.equal(rendered.claims.length,14);ctx.PICK.selected.clear();await ctx.runVerify();assert.equal(requests,1);console.log('ICT 14-item submission and empty-selection guard passed');})().catch(error=>{console.error(error);process.exitCode=1;});
