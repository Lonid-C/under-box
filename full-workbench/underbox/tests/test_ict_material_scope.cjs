const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
class Node {constructor(){this.children=[];this.handlers={};this.value='';}append(...v){this.children.push(...v);}addEventListener(k,f){this.handlers[k]=f;}setAttribute(){}replaceChildren(){this.children=[];}close(){}showModal(){}}
const ids=new Map();const byId=id=>{if(!ids.has(id))ids.set(id,new Node());return ids.get(id);};
const pick={token:'scope-a',claims:[{id:'c1',githubMaterialEligible:true},{id:'c2',githubMaterialEligible:false}],selected:new Set(['c1','c2'])};
const context={document:{getElementById:byId,createElement:()=>new Node()},window:{},PICK:pick,renderPick:()=>byId('pickBlocks').replaceChildren()};
vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../ict-ui.js'),'utf8'),context);
context.renderPick();assert.equal(byId('pickBlocks').children.length,1,'only explicitly eligible project shows material inputs');
const row=byId('pickBlocks').children[0],account=row.children[2],urls=row.children[3];account.value='https://github.com/synthetic-user';urls.value='https://github.com/synthetic-user/repo';account.handlers.input();
assert.equal(context.window.ictGithubMaterials().c1.urls[0],urls.value);
pick.selected.clear();assert.equal(Object.keys(context.window.ictGithubMaterials()).length,0,'unselected claim cannot submit its supplements');
pick.selected.add('c1');pick.token='scope-b';assert.equal(Object.keys(context.window.ictGithubMaterials()).length,0,'new split cannot reuse old materials before render');
context.renderPick();assert.equal(byId('pickBlocks').children[0].children[2].value,'');assert.equal(Object.keys(context.window.ictGithubMaterials()).length,0,'same c1 id in another resume cannot inherit previous links');
console.log('ICT GitHub material scope tests passed');
