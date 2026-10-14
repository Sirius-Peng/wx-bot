'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const ROOT=process.env.WXBOT_TEST_ROOT||path.resolve(__dirname,'..');
const app=fs.readFileSync(path.join(ROOT,'src/web/app.js'),'utf8');
function fixture(){
 const nodes=new Map();function node(id){if(!nodes.has(id))nodes.set(id,{value:'',hidden:true,disabled:false,checked:false,textContent:'',addEventListener(){}});return nodes.get(id)}
 const c={document:{querySelector:node,querySelectorAll:()=>[],addEventListener(){}},window:{addEventListener(){}},console,setTimeout,clearTimeout,setInterval(){},clearInterval,confirmAction:async()=>true};
 vm.createContext(c);vm.runInContext(app.slice(0,app.lastIndexOf('\ninit().catch')),c);return {c,node,run:s=>vm.runInContext(s,c)};
}
async function tests(){let count=0;
 {
  const f=fixture();f.node('#import-source').value='/old';f.run(`api=async()=>({source:'/old',token:'fixture',files:8,directories:2,bytes:1024,includes:{persona:true,memory:true},message:'fixture'});`);
  await f.run('previewImportDirectory()');assert.equal(f.node('#import-preview').hidden,false);assert.equal(f.node('#btn-import-prepare').disabled,true);assert(f.node('#import-summary').textContent.includes('长期记忆'));count++;
 }
 {
  const f=fixture();f.c.calls=0;f.run(`api=async()=>{calls++;return {}};IMPORT.preview={token:'fixture'};`);await f.run('prepareImportDirectory()');assert.equal(f.c.calls,0);count++;
 }
 {
  const f=fixture();f.node('#import-confirm').checked=true;f.c.payload=null;f.run(`IMPORT.preview={token:'fixture'};api=async(p,o)=>{payload=JSON.parse(o.body);return {import:{status:'queued',message:'queued'}}};`);
  await f.run('prepareImportDirectory()');assert.deepEqual(JSON.parse(JSON.stringify(f.c.payload)),{token:'fixture',confirm_replace:true});assert.equal(f.node('#import-source').disabled,true);assert.equal(f.node('#btn-import-cancel').hidden,false);assert(f.node('#import-status').textContent.includes('重启机器人'));count++;
 }
 {
  const f=fixture();f.c.window.wxbotDesktop={data:{},backend:{}};f.run(`renderImportStatus({status:'queued',message:'fixture'})`);assert.equal(f.node('#btn-import-restart').hidden,false);assert.equal(f.node('#btn-import-choose').hidden,false);count++;
 }
 {
  const f=fixture();f.node('#import-source').value='/old';f.run(`api=async()=>await new Promise(r=>resolvePreview=r);`);const p=f.run('previewImportDirectory()');f.run('invalidateImportPreview()');f.c.resolvePreview({source:'/old',includes:{},files:1,directories:0,bytes:0});await p;assert.equal(f.node('#import-preview').hidden,true);count++;
 }
 {
  const f=fixture();f.c.calls=0;f.c.window.wxbotDesktop={data:{chooseLegacyDir:async()=>({canceled:true})}};f.run(`api=async()=>{calls++;return {}}`);await f.run('chooseImportDirectory()');assert.equal(f.c.calls,0);count++;
 }
 {
  const f=fixture();f.node('#import-source').value='/old';f.run(`api=async()=>{throw new Error('fixture error')};`);await f.run('previewImportDirectory()');assert(f.node('#import-status').textContent.includes('fixture error'));assert.equal(f.node('#btn-import-preview').disabled,false);assert.equal(f.node('#btn-import-prepare').disabled,true);count++;
 }
 {
  const f=fixture();f.run(`IMPORT.queued=true;api=async()=>({message:'cancelled'});`);await f.run('cancelImportDirectory()');assert.equal(f.node('#btn-import-cancel').hidden,true);assert.equal(f.node('#import-source').disabled,false);count++;
 }
 {
  const f=fixture();f.c.calls=0;f.c.window.wxbotDesktop={backend:{restart:async()=>{f.c.calls++;return {ok:false,error:'fixture restart failure'}}}};f.run(`IMPORT.queued=true;`);await f.run('restartForImport()');assert.equal(f.c.calls,1);assert(f.node('#import-status').textContent.includes('fixture restart failure'));assert.equal(f.node('#btn-import-restart').disabled,false);count++;
 }
 // The native chooser rejects external pages, frames and unrelated windows.
 {
  const handlers={};let dialogs=0;const mainFrame={url:'http://127.0.0.1:17800/'};const sender={mainFrame};const mod={exports:{}};
  vm.runInNewContext(fs.readFileSync(path.join(ROOT,'desktop/src/main/ipc.js'),'utf8'),{module:mod,URL,require:n=>n==='electron'?{ipcMain:{handle:(n,f)=>handlers[n]=f},app:{},shell:{},dialog:{showOpenDialog:async()=>{dialogs++;return {canceled:false,filePaths:['C:\\旧数据目录']}}}}:{}});
  mod.exports.registerIpc({windowMgr:{win:{webContents:sender},restoreInputFocus(){}},backend:{url:'http://127.0.0.1:17800'},trayMgr:{},quit(){}});
  let r=await handlers['data:chooseLegacyDir']({sender,senderFrame:mainFrame});assert.equal(r.path,'C:\\旧数据目录');assert.equal(dialogs,1);
  mainFrame.url='https://external.example/';r=await handlers['data:chooseLegacyDir']({sender,senderFrame:mainFrame});assert(r.canceled);assert.equal(dialogs,1);
  mainFrame.url='http://127.0.0.1:17800/';r=await handlers['data:chooseLegacyDir']({sender,senderFrame:{url:mainFrame.url}});assert(r.canceled);assert.equal(dialogs,1);count++;
 }
 console.log(`DATA_IMPORT_FRONTEND PASS tests=${count} chooser=1 preview_confirmation=1 no_stale_preview=1 restart_retry=1`);
}
tests().catch(e=>{console.error(e);process.exitCode=1});
