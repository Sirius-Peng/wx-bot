'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');const vm=require('node:vm');
const root=path.resolve(__dirname,'..');
const app=fs.readFileSync(path.join(root,'src/web/app.js'),'utf8').split('init().catch')[0];
const guide=fs.readFileSync(path.join(root,'src/web/guide.js'),'utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function fixture(){
  const nodes=new Map();
  function node(id){if(!nodes.has(id))nodes.set(id,{id,hidden:true,disabled:false,value:'',textContent:'',innerHTML:'',checked:false,children:[],style:{},dataset:{},classList:{toggle(){},add(){},remove(){}},addEventListener(){},setAttribute(){},removeAttribute(){},focus(){},replaceChildren(...items){this.children=items;}});return nodes.get(id);}
  const context={console,location:{hostname:'127.0.0.1',port:'8000',hash:''},history:{replaceState(){}},navigator:{},window:{addEventListener(){}},document:{querySelector:node,querySelectorAll(){return[]},addEventListener(){},createElement(){return {set textContent(value){this.innerHTML=String(value)}}}},setInterval(){return 1;},clearInterval(){},setTimeout,Promise,Date,Image:class{constructor(){context.imageCount++}decode(){return new Promise(resolve=>context.decodeDone=resolve)}},imageCount:0,confirmAction:async()=>true};
  vm.createContext(context);vm.runInContext(guide+'\n'+app,context);
  vm.runInContext(`STATE.config={adapter:{qq_enabled:true,napcat_webui_token:''},llm:{},bot:{}};loadConfig=async()=>{};refreshOnboarding=async()=>({});refreshStatus=async()=>{};toast=()=>{};`,context);
  return {context,node,run:script=>vm.runInContext(script,context)};
}
async function tests(){
  let count=0;
  // Repeated clicks share one gateway launch; online session does not fetch QR.
  {
    const f=fixture();f.context.apiCalls=0;f.context.done=0;
    f.run(`refreshQQStatus=async()=>({manager:{mode:'native',supported:true,installed:true,running:true},napcat:{running:true},task:{},loggedIn:true});pollQQStatus=refreshQQStatus;finishQQLogin=async()=>done++;api=async(path)=>{apiCalls++;return await new Promise(resolve=>releaseSetup=()=>resolve({ok:true}));};`);
    const a=f.run('setupQQ({saveForm:false})'),b=f.run('setupQQ({saveForm:false})');await tick();assert.equal(f.context.apiCalls,1);f.context.releaseSetup();await Promise.all([a,b]);assert.equal(f.context.done,1);assert.equal(f.context.imageCount,0);count++;
  }
  // Homepage saves only the QQ enable delta, never hidden model/persona drafts.
  {
    const f=fixture();f.context.patchBody=null;
    f.run(`STATE.config.adapter.qq_enabled=false;refreshQQStatus=async()=>({manager:{mode:'native',supported:true,installed:true,running:true},napcat:{running:true},task:{},loggedIn:true});pollQQStatus=refreshQQStatus;finishQQLogin=async()=>{};api=async(path,opts)=>{if(path.includes('/section/')){patchBody=JSON.parse(opts.body);return {config:{adapter:{qq_enabled:true}}}}return {ok:true};};`);
    assert.equal(await f.run('setupQQ({saveForm:false})'),true);assert.deepEqual(JSON.parse(JSON.stringify(f.context.patchBody)),{qq_enabled:true});count++;
  }
  // QR modal stays closed through API wait AND actual image decoding.
  {
    const f=fixture();f.context.qrCalls=0;f.run(`api=async()=>{qrCalls++;return await new Promise(resolve=>qrReady=()=>resolve({ok:true,image:'data:fixture'}));};`);
    const a=f.run('getQQQr()'),b=f.run('getQQQr()');await tick();assert.equal(f.context.qrCalls,1);assert.equal(f.node('#qq-login-modal').hidden,true);
    f.context.qrReady();await tick();assert.equal(f.context.imageCount,1);assert.equal(f.node('#qq-login-modal').hidden,true);f.context.decodeDone();await Promise.all([a,b]);assert.equal(f.node('#qq-login-modal').hidden,false);count++;
  }
  // Escape while generation is pending prevents a late surprise dialog.
  {
    const f=fixture();f.run(`api=async()=>await new Promise(resolve=>qrReady=()=>resolve({ok:true,image:'data:fixture'}));`);
    const pending=f.run('getQQQr()');f.run('closeQQDialogs()');f.context.qrReady();assert.equal(await pending,false);assert.equal(f.node('#qq-login-modal').hidden,true);assert.equal(f.context.imageCount,0);count++;
  }
  {
    const f=fixture();f.run(`api=async()=>({ok:false,pending:true,message:'waiting'});`);assert.equal(await f.run('getQQQr()'),false);assert.equal(f.node('#qq-login-modal').hidden,true);count++;
  }
  {
    const f=fixture();f.context.done=0;f.run(`api=async()=>({ok:true,loggedIn:true});finishQQLogin=async()=>done++;`);assert.equal(await f.run('getQQQr()'),true);assert.equal(f.context.done,1);assert.equal(f.context.imageCount,0);count++;
  }
  // Saved accounts offer a chooser, rather than forcing a new scan.
  {
    const f=fixture();f.context.qrs=0;f.run(`loadQQAccounts=async()=>[{qq:'12345',nickname:'fixture'}];getQQQr=async()=>qrs++;`);await f.run('beginQQLogin()');assert.equal(f.node('#qq-choice-modal').hidden,false);assert.equal(f.context.qrs,0);count++;
  }
  {
    const f=fixture();f.context.quick=0;f.context.qrs=0;f.run(`STATE.qqAutoLoginQq='12345';loadQQAccounts=async()=>[{qq:'12345'}];quickLoginQQ=async(opts)=>{quick++;return opts.qq==='12345'};getQQQr=async()=>qrs++;`);await f.run('beginQQLogin()');assert.equal(f.context.quick,1);assert.equal(f.context.qrs,0);count++;
  }
  {
    const f=fixture();f.context.qrs=0;f.run(`loadQQAccounts=async()=>[];getQQQr=async()=>qrs++;`);await f.run('beginQQLogin()');assert.equal(f.context.qrs,1);count++;
  }
  // Existing key stays server-side when the user leaves the guide key field empty.
  {
    const f=fixture();f.context.calls=[];
    f.node('#guide-model-base').value='https://api.deepseek.com';f.node('#guide-model-name').value='deepseek-flash';
    f.run(`GUIDE.state={keyConfigured:true};STATE.config.llm={primary_base:'https://api.deepseek.com'};api=async(path,opts)=>{calls.push([path,JSON.parse(opts.body||'{}')]);return path.includes('/section/')?{config:STATE.config}:{ok:true,message:'verified'};};`);
    await f.run('saveGuideModel()');assert.equal(f.context.calls.length,2);assert.equal(f.context.calls[0][1].primary_key,undefined);assert.equal(f.context.calls[1][0],'/api/llm/test');count++;
  }
  // A different provider requires its own key, never silently reusing a saved one.
  {
    const f=fixture();f.context.calls=0;f.node('#guide-model-base').value='https://different.example/v1';f.node('#guide-model-name').value='other';
    f.run(`GUIDE.state={keyConfigured:true};STATE.config.llm={primary_base:'https://api.deepseek.com'};api=async()=>{calls++;return {ok:true}};`);
    await f.run('saveGuideModel()');assert.equal(f.context.calls,0);assert.equal(f.node('#btn-guide-model').disabled,false);count++;
  }
  assert.equal(count,11);
  console.log('GUIDE_FRONTEND PASS tests=11 single_start=1 home_delta_only=1 qr_ready_then_modal=1 no_late_modal=1 saved_login_preserved=1');
}
tests().catch(e=>{console.error(e);process.exitCode=1});
