const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname,'../index.html'),'utf8');
const source = html.slice(html.indexOf('function renderManualReview(review)'), html.indexOf('\nfunction ',html.indexOf('function renderManualReview(review)') + 10));
assert.ok(source.startsWith('function renderManualReview(review)'));
const el=(tag,attrs={},...kids)=>({tag,attrs,kids:kids.flat().filter(Boolean),appendChild(child){this.kids.push(child);}});
const context=vm.createContext({el,URL});
vm.runInContext(source+';globalThis.render=renderManualReview;',context);
const visit=node=>[node,...(node.kids || []).flatMap(child=>typeof child==='object'?visit(child):[])];
const review={company:'合成公司有限公司',queryUrl:'https://www.qcc.com/web/search?key=synthetic',companyUrl:'',contacts:[],note:'公司信息不证明实习',checks:['核对岗位'],limitations:[]};
let nodes=visit(context.render(review));
assert.ok(nodes.some(n=>n.tag==='a' && n.attrs.href===review.queryUrl));
assert.ok(nodes.some(n=>n.attrs.text?.includes('未取得可核实出处')));
review.contacts=[{phone:'010-12345678',excerpt:'电话：010-12345678',sourceUrl:'https://www.qcc.com/firm/synthetic.html',accessedAt:'2026-10-12'}];
nodes=visit(context.render(review));
assert.ok(nodes.some(n=>n.attrs.text?.includes('010-12345678')));
assert.ok(nodes.some(n=>n.attrs.text==='原文：电话：010-12345678'));
assert.equal(nodes.filter(n=>n.tag==='a').length,2);
for(const malicious of ['javascript:alert(1)','https://www.qcc.com.evil.test/firm/a.html','https://user@www.qcc.com/firm/a.html']) {
  review.queryUrl=malicious;review.contacts[0].sourceUrl=malicious;
  assert.equal(visit(context.render(review)).filter(n=>n.tag==='a').length,0);
}
assert.ok(html.includes("'实习待核查'"));
assert.ok(html.includes("cats: ['实习']"));
console.log('Manual-review renderer: company links, sourced phones, missing-phone fallback, unsafe-link rejection and distinct internship entry passed.');

// Execute original context renderers together; catches accidental self-recursion
// and verifies modern email/profile data remains visible after migration.
const extract=name=>{const start=html.indexOf('function '+name+'(');const end=html.indexOf('\n}',start+10)+2;return html.slice(start,end);};
const complete=vm.createContext({el,URL,Set});
for(const name of ['contextLink','renderContextDetails','renderEducationReview'])vm.runInContext(extract(name),complete);
const education={kind:'education',school:'合成大学',links:[],profile:{introduction:{label:'专业介绍',value:'合成专业介绍',sourceUrl:'https://school.example/program'}},contacts:[{type:'email',value:'office@example.edu',sourceUrl:'https://school.example/contact',excerpt:'office@example.edu'}],limitations:[]};
const contextNodes=visit(complete.renderEducationReview(education));
assert.ok(contextNodes.some(n=>n.attrs.text?.includes('合成专业介绍')));
assert.ok(contextNodes.some(n=>n.attrs.text?.includes('office@example.edu')));
console.log('Full context rendering: no recursion, source links and email remain visible.');
