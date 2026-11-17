const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../index.html'),'utf8');
const source=html.slice(html.indexOf('const Chat = {'),html.indexOf('function initChat()'))+';this.chat=Chat;';
let posted=[],committed=0;const encoded=new TextEncoder().encode('data: {"delta":"DeepSeek 辅助分析"}\n\ndata: {"done":true,"seconds":0.1,"usage":{}}\n\n');
const ctx={TextDecoder,AbortController,DOMException,QUERY_EPOCH:1,$:()=>({checked:true}),Usage:{start:()=>1,commit:()=>committed++,live(){}},async fetch(url,init){posted.push(JSON.parse(init.body));let read=false;return {ok:true,headers:{get:()=> 'text/event-stream'},body:{getReader:()=>({read:async()=>read?{done:true}:(read=true,{done:false,value:encoded})})}};}};
vm.createContext(ctx);vm.runInContext(source,ctx);
(async()=>{
 let text='';await ctx.chat.askStream('分析 c1',t=>text=t,()=>{},()=>{},{diagnoseClaimId:'c1'});
 assert.equal(posted[0].diagnoseClaimId,'c1');assert.equal(posted[0].webSearch,false);assert.equal(text,'DeepSeek 辅助分析');assert.equal(committed,1);
 await ctx.chat.askStream('普通联网问答',()=>{},()=>{},()=>{});assert.equal(posted[1].webSearch,true);assert.equal(posted[1].diagnoseClaimId,undefined);assert.equal(committed,2);
 console.log('Diagnostic chat sends one saved-record request with web search disabled; normal QA preserved');
})().catch(e=>{console.error(e);process.exitCode=1;});
