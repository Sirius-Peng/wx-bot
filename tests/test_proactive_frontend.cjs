'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=process.env.WXBOT_TEST_ROOT||path.resolve(__dirname,'..');
const source=fs.readFileSync(path.join(root,'src/web/app.js'),'utf8');
const body=source.slice(source.indexOf('async function loadProactive()'),source.indexOf('async function saveSection('));
class Node {
 constructor(tag='div'){this.tag=tag;this.children=[];this.listeners={};this.attributes={};this.textContent='';this.checked=false;this.disabled=false;}
 append(...nodes){this.children.push(...nodes);}replaceChildren(){this.children=[];this.textContent='';}
 addEventListener(type,cb){this.listeners[type]=cb;}setAttribute(k,v){this.attributes[k]=v;}
}
function fixture(data){
 const container=new Node(),calls=[],toasts=[];
 const context={Date,JSON,document:{createElement:t=>new Node(t)},$:()=>container,
   toast:(...x)=>toasts.push(x),api:async(url,opts)=>{calls.push({url,opts});return data;}};
 vm.createContext(context);vm.runInContext(body,context);
 return {container,calls,toasts,context,load:()=>context.loadProactive()};
}
const person={id:'ilink:wx',enabled:true,muted:false,awaiting_reply:false,next_at:1792036800,last_status:'',accounts:[{name:'用户',owner_id:'wx',channel:'微信'},{name:'用户',owner_id:'123',channel:'QQ'}]};
let tests=0;
(async()=>{
 {const f=fixture({enabled:false,contacts:[]});await f.load();assert(f.container.textContent.includes('先通过微信或 QQ'));tests++;}
 {const f=fixture({enabled:true,contacts:[person]});await f.load();assert.equal(f.container.children.length,1);const [info,toggle]=f.container.children[0].children;assert(info.children[0].textContent.includes('↔'));assert(info.children[1].textContent.includes('计划不早于'));assert.equal(toggle.checked,true);assert.equal(toggle.attributes['aria-label'].includes('允许主动联系'),true);tests++;}
 {const f=fixture({enabled:true,contacts:[{...person,accounts:[{name:'<img src=x onerror=bad()>',owner_id:'wx',channel:'微信'}]}]});await f.load();const title=f.container.children[0].children[0].children[0];assert(title.textContent.includes('<img'));assert.equal(title.children.length,0);tests++;}
 {const f=fixture({enabled:true,contacts:[{...person,muted:true}]});await f.load();assert.equal(f.container.children[0].children[1].checked,false);assert(f.container.children[0].children[0].children[1].textContent.includes('暂停'));tests++;}
 {const f=fixture({enabled:true,contacts:[{...person,awaiting_reply:true,last_status:'uncertain'}]});await f.load();assert(f.container.children[0].children[0].children[1].textContent.includes('重复提交'));tests++;}
 {const f=fixture({enabled:true,contacts:[person]});await f.load();const toggle=f.container.children[0].children[1];toggle.checked=false;await toggle.listeners.change();const call=f.calls.find(x=>x.opts);assert.equal(call.url,'/api/proactive/contact');assert.deepEqual(JSON.parse(call.opts.body),{id:'ilink:wx',enabled:false});assert.equal(toggle.disabled,false);tests++;}
 {const f=fixture({enabled:true,contacts:[person]});await f.load();f.context.api=async()=>{throw Error('fixture');};const toggle=f.container.children[0].children[1];toggle.checked=false;await toggle.listeners.change();assert.equal(toggle.checked,true);assert.equal(toggle.disabled,false);assert(f.toasts[0][0].includes('保存失败'));tests++;}
 {const f=fixture({enabled:true,state_error:true,contacts:[]});await f.load();assert(f.container.textContent.includes('已暂停发送'));tests++;}
 {const html=fs.readFileSync(path.join(root,'src/web/index.html'),'utf8');assert(html.includes('data-save-section="proactive"'));assert(source.includes("proactive: ['bot'"));for(const id of ['proactive-enabled','proactive-min-hours','proactive-max-hours','proactive-daily-limit','proactive-quiet-start','proactive-quiet-end'])assert(html.includes('id="'+id+'"'));tests++;}
 {const html=fs.readFileSync(path.join(root,'src/web/index.html'),'utf8');assert(html.includes('只用微信或 QQ 也能直接使用，无需关联身份'));assert(!html.includes('先在「人格知识库 → 微信 ↔ QQ 身份关联」绑定同一人的两个账号'));tests++;}
 {const solo={...person,awaiting_reply:true,last_status:'accepted',accounts:[person.accounts[0]]};const f=fixture({enabled:true,contacts:[solo]});await f.load();const [info,toggle]=f.container.children[0].children;assert(info.children[0].textContent.includes('微信'));assert(!info.children[0].textContent.includes('↔'));assert(!info.children[1].textContent.includes('两端'));assert(toggle.checked);tests++;}
 {const start=source.indexOf("  $$('[data-settings-section]').forEach(card=>{");const end=source.indexOf("  $('#search-backend')",start);assert(start>0&&end>start);const handlers={},dirty=[];const card={dataset:{settingsSection:'proactive'},addEventListener:(name,fn)=>handlers[name]=fn};vm.runInNewContext(source.slice(start,end),{$$:()=>[card],markSectionDirty:name=>dirty.push(name)});for(const event of ['input','change']){handlers[event]({target:{id:'proactive-enabled',matches:()=>true}});}assert.deepEqual(dirty,['proactive','proactive']);handlers.input({target:{id:'label',matches:()=>false}});handlers.input({target:{id:'',matches:()=>true}});assert.equal(dirty.length,2);tests++;}
 console.log(`proactive frontend PASS tests=${tests}`);
})().catch(e=>{console.error(e);process.exitCode=1;});
