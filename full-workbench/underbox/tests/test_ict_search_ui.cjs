const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../index.html'),'utf8');
class Node{constructor(text=''){this.text=String(text);this.children=[];}appendChild(n){this.children.push(n);return n;}get textContent(){return this.text+this.children.map(n=>n.textContent).join('');}set innerHTML(v){throw Error('untrusted markup');}}
const ctx={STATUS:{none:{label:'未找到公开记录'}},el:(tag,props={},...children)=>{const n=new Node(props.text||'');children.forEach(c=>n.appendChild(c));return n;}};
vm.createContext(ctx);vm.runInContext(html.slice(html.indexOf('function retrievalLabel('),html.indexOf('function detailOf(')),ctx);
assert.equal(ctx.retrievalLabel({status:'none',retrievalStatus:'not_started'}),'待补充检索信息');
assert.equal(ctx.retrievalLabel({status:'none'}),'未找到公开记录');
assert.equal(ctx.retrievalDetails({}),null);
const c={verificationAgent:{sourceAccess:{queryPlan:{state:'ready',queryCount:2},decisionCounts:{'正文缺少本人锚点':3},decisions:[{reason:'正文缺少本人锚点',reference:'https://example.test/<script>alert(1)</script>'}],decisionsTruncated:true}}};
const box=ctx.retrievalDetails(c);assert.match(box.textContent,/已生成 2 条查询/);assert.match(box.textContent,/正文缺少本人锚点：3/);assert.match(box.textContent,/逐页记录已达显示上限/);
c.verificationAgent.sourceAccess.queryPlan.state='no_queries';assert.match(ctx.retrievalDetails(c).textContent,/未生成可用查询/);
console.log('ICT search state, rejection counts and safe text rendering passed');
