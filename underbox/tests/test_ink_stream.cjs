/* Run with node --test underbox/tests/test_ink_stream.cjs. No DOM dependency. */
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function renderer(reduced = false) {
  let id = 0, time = 0;
  const frames = new Map();
  class Node {
    constructor() {this.children=[];this.attributes={};this.isConnected=true;this._text='';this.classes=new Set();
      this.classList={add:(...xs)=>xs.forEach(x=>this.classes.add(x)),remove:(...xs)=>xs.forEach(x=>this.classes.delete(x))};}
    set textContent(value){this._text=String(value);this.children=[];}
    get textContent(){return this._text+this.children.map(n=>typeof n==='string'?n:n.textContent).join('');}
    set innerHTML(_){throw new Error('Model content must never be assigned to innerHTML');}
    append(...nodes){this.children.push(...nodes);}
    replaceChildren(...nodes){this._text='';this.children=nodes;}
    setAttribute(key,value){this.attributes[key]=value;}
    addEventListener(){}
    normalize(){}
  }
  const ctx = {
    document:{readyState:'loading',addEventListener(){},createElement:()=>new Node(),createTextNode:value=>({textContent:value})},
    matchMedia:()=>({matches:reduced}),requestAnimationFrame:fn=>{frames.set(++id,fn);return id;},
    cancelAnimationFrame:id=>frames.delete(id),setTimeout,clearTimeout,
  };
  ctx.window=ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../ui.js'),'utf8'),ctx);
  const drain=(limit=10000)=>{
    while(frames.size && limit--){const pending=[...frames.values()];frames.clear();time+=32;pending.forEach(fn=>fn(time));}
    assert.ok(limit>0,'Renderer must finish without an unbounded animation loop');
  };
  const step=()=>{const pending=[...frames.values()];frames.clear();time+=32;pending.forEach(fn=>fn(time));};
  return {InkStream:ctx.InkStream,Node,drain,step,frames};
}

test('cumulative chunks render Chinese, emoji and newlines exactly once',async()=>{
  const env=renderer(),host=new env.Node(),painter=new env.InkStream(host);
  painter.status('整理中');
  painter.update('第一条：');
  env.step();
  painter.update('第一条：公开来源 🧸\n第二条：保持未知。');
  const done=painter.finish();env.drain();await done;
  assert.equal(host.textContent,'第一条：公开来源 🧸\n第二条：保持未知。');
  assert.equal(host.attributes['aria-busy'],'false');
  assert.equal(env.InkStream.active.size,0);
});

test('stop freezes already visible text and settles a pending finish promise',async()=>{
  const env=renderer(),host=new env.Node(),painter=new env.InkStream(host);
  const full='可追溯的公开证据。'.repeat(400);
  painter.update(full);env.step();
  const visible=host.textContent;
  const done=painter.finish();painter.cancel();env.drain();await done;
  assert.ok(visible.length>0 && visible.length<full.length);
  assert.equal(host.textContent,visible);
  painter.update('late data must be ignored');
  assert.equal(host.textContent,visible);
  assert.equal(env.frames.size,0);
});

test('reduced motion paints real content immediately without animation frames',async()=>{
  const env=renderer(true),host=new env.Node(),painter=new env.InkStream(host);
  painter.update('减少动态效果：完整回答。');
  assert.equal(host.textContent,'减少动态效果：完整回答。');
  assert.equal(env.frames.size,0);
  await painter.finish();
  assert.equal(env.InkStream.active.size,0);
});

test('markup from a model stays literal text',async()=>{
  const env=renderer(),host=new env.Node(),painter=new env.InkStream(host);
  const payload='<img src=x onerror=alert(1)> **证据**';
  painter.update(payload);
  const done=painter.finish();env.drain();await done;
  assert.equal(host.textContent,payload);
});

test('query reset cancels every active renderer and detached nodes stop work',()=>{
  const env=renderer(),a=new env.Node(),b=new env.Node();
  const one=new env.InkStream(a),two=new env.InkStream(b);
  one.update('回答一'.repeat(100));two.update('回答二'.repeat(100));
  env.InkStream.cancelAll();env.drain();
  assert.equal(env.InkStream.active.size,0);
  assert.equal(env.frames.size,0);
  const detached=new env.Node(),three=new env.InkStream(detached);
  three.update('detached');detached.isConnected=false;env.drain();
  assert.equal(env.InkStream.active.size,0);
});
