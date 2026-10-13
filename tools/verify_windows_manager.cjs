/* Native process/HTTP integration, no browser automation or real accounts. */
'use strict';
const fs=require('fs'),path=require('path'),os=require('os'),vm=require('vm'),net=require('net'),assert=require('assert');
const ROOT=path.resolve(__dirname,'..'),binary=path.resolve(process.argv[2]),report=path.resolve(process.argv[3]);
const temp=fs.mkdtempSync(path.join(os.tmpdir(),'wxbot 管理 中文 ')),data=path.join(temp,'用户 data');fs.mkdirSync(path.join(data,'logs'),{recursive:true});
const source=fs.readFileSync(path.join(ROOT,'desktop/src/main/backend.js'),'utf8');
async function main(){
 const occupied=net.createServer();await new Promise(r=>occupied.listen(0,'127.0.0.1',r));const port=occupied.address().port;
 const mod={exports:{}};const env={...process.env,WXBOT_PREFERRED_PORT:String(port),WXBOT_DATA_DIR:data,WXBOT_SKIP_NAPCAT_SYNC:'1',WXBOT_MIGRATE_LEGACY:'0'};
 vm.runInNewContext(source,{require:n=>n==='./paths'?{resolveBackend:()=>({kind:'binary',command:binary,args:[],cwd:ROOT}),userDataDir:()=>data,logDir:()=>path.join(data,'logs'),legacyDataDirs:()=>[]}:require(n),module:mod,process:{platform:'win32',env,cwd:()=>ROOT},setTimeout,clearTimeout},{filename:'backend.js'});
 const b=new mod.exports.Backend(),errors=[];b.on('error',e=>errors.push(e));
 const start=Date.now();let summary;
 try{
  const promise=b.start();assert.strictEqual(b.start(),promise);const r=await promise;assert(r.ok,r.error);assert.notStrictEqual(r.port,port);
  const startupMs=Date.now()-start;
  let api=await fetch(b.url+'/api/config');assert.strictEqual(api.status,401);
  const auth={Authorization:`Bearer ${b.controlToken}`};const check=async(route)=>{const r=await fetch(b.url+route,{headers:auth});assert.strictEqual(r.status,200,route);return r.json();};
  await check('/api/onboarding');const state=await check('/api/data/status');assert(JSON.stringify(state).includes(data.replaceAll('\\','\\\\'))||JSON.stringify(state).includes(data));
  const add=await fetch(b.url+'/api/memory',{method:'POST',headers:{...auth,'Content-Type':'application/json'},body:JSON.stringify({scope:'user',owner_id:'fixture',category:'fact',content:'测试记忆 中文',keywords:['测试']})});assert((await add.json()).ok);
  const restart=b.restart();assert.strictEqual(b.restart(),restart);assert((await restart).ok);
  const memory=await check('/api/memory/stats');assert.strictEqual(memory.total,1);
  const config=await check('/api/config');assert(config.bot);
  const activePort=b.port;await b.stop();assert.strictEqual(b.proc,null);assert.strictEqual(b.state,'stopped');
  const stopped=await new Promise(resolve=>{const s=net.connect(activePort,'127.0.0.1');s.on('connect',()=>{s.destroy();resolve(false);});s.on('error',()=>resolve(true));});assert(stopped,'listener leaked');
  assert(fs.existsSync(path.join(data,'memory.json')));assert.strictEqual(errors.length,0);
  summary='WINDOWS_MANAGER PASS start_coalesced=1 port_fallback=1 auth=1 onboarding=1 data_path=1 memory_restart=1 graceful_stop=1 listener_closed=1';
  fs.writeFileSync(report,JSON.stringify({summary,exit_status:0,startup_ms:startupMs,backend:binary},null,2));console.log(summary);
 }finally{await b.stop();occupied.close();b._logStream?.end();setTimeout(()=>fs.rmSync(temp,{recursive:true,force:true}),200);}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
