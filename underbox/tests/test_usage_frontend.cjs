const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname,'../index.html'),'utf8');
// Parse all current inline JavaScript so errors outside the tested objects are caught.
let parsedScripts=0;
for (const [,attrs,source] of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
  if (/\bsrc\s*=/.test(attrs) || !source.trim()) continue;
  const type=attrs.match(/\btype\s*=\s*["']([^"']+)["']/)?.[1];
  if (type && !['text/javascript','application/javascript'].includes(type)) continue;
  new vm.Script(source,{filename:`index.html:inline-${++parsedScripts}`});
}
assert.ok(parsedScripts>0,'Main inline JavaScript must be parsed');
const code = html.slice(html.indexOf('const Usage = {'),html.indexOf("document.getElementById('usageHead')"));
const context = vm.createContext({Map,Number,document:{getElementById:()=>({classList:{toggle(){}},innerHTML:''})}});
vm.runInContext(code+'; globalThis.usage=Usage;',context);
const u=context.usage;
const amount=(n, cost=n*.01)=>({deepseek:{calls:n,in:n*100,out:n*10,tokens:n*110,yuan:cost},glm:{searches:n,searchYuan:n*.01,yuan:n*.01}});
u.reset();const a=u.start(),b=u.start();u.live(amount(3),a);u.live(amount(7),b);
assert.equal(u.total().deepseek.calls,10);
u.commit(b);assert.equal(u.total().deepseek.calls,10);assert.equal(u.active.size,1);
u.live(amount(4),a);assert.equal(u.total().deepseek.calls,11);
u.commit(a);u.commit(a);assert.equal(u.total().deepseek.calls,11);assert.equal(u.active.size,0);
const old=u.start();u.live(amount(9),old);u.reset();const fresh=u.start();u.live(amount(5),fresh);u.live(amount(99),old);u.commit(old);
assert.equal(u.total().deepseek.calls,5);u.commit(fresh);
assert.equal(u.total().deepseek.calls,5);
u.reset();const unknown=u.start();const d=amount(2,null);d.glm.yuan=null;u.live(d,unknown);u.commit(unknown);
assert.equal(u.total().deepseek.yuan,null);assert.equal(u.total().glm.yuan,null);
assert.equal(u.total().glm.searchYuan,.02);
const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);
assert.equal(new Set(ids).size,ids.length,'No duplicate IDs from visual brand layers');
for(const id of ['navClaims','navTier','navHistory','heroHistory','historyDialog']) assert.ok(ids.includes(id),id);
assert.ok(html.includes("fetch('/api/query/reset'"));assert.ok(html.includes('StatusDock.clear()'));
assert.ok(html.includes('grid-template-columns') && html.includes('flex-wrap:wrap'));

// Execute the real clear method; merely finding its name would miss stale logs/timers.
const nodes = new Map();
for (const id of ['spLog','spSteps','spTime','spPillTxt','spFoot','statusDock','statusPanel','statusPill']) {
  const classes = new Set(['done','failed','collapsed']);
  nodes.set(id, {children:['old query'],textContent:'old query',hidden:false,
    replaceChildren(){this.children=[];},classList:{remove(...names){names.forEach(n=>classes.delete(n));}},classes});
}
let clearedTimer;
const dockContext = vm.createContext({document:{getElementById:id=>nodes.get(id)},
  clearInterval:id=>{clearedTimer=id;}});
const dockStart = html.indexOf('const StatusDock = {');
assert.ok(dockStart >= 0);
const dockEnd = html.indexOf('\n};',dockStart);
assert.ok(dockEnd > dockStart);
vm.runInContext(html.slice(dockStart,dockEnd+3)+';globalThis.dock=StatusDock;',dockContext);
dockContext.dock.timer=123;dockContext.dock.started=999;
dockContext.dock.clear();
assert.equal(clearedTimer,123);
assert.equal(dockContext.dock.timer,null);
assert.equal(dockContext.dock.started,0);
for (const id of ['spLog','spSteps']) assert.equal(nodes.get(id).children.length,0,id);
for (const id of ['spTime','spPillTxt','spFoot']) assert.equal(nodes.get(id).textContent,'',id);
for (const id of ['statusDock','statusPanel','statusPill']) assert.equal(nodes.get(id).hidden,true,id);
assert.equal(nodes.get('statusDock').classes.size,0);
dockContext.dock.clear(); // Idempotent when a previous query is already gone.
console.log('Frontend JavaScript syntax, usage, actual log/timer clearing, IDs, navigation and history checks passed.');
