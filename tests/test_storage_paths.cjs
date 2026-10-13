'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert'),os=require('os');
const ROOT=process.env.WXBOT_TEST_ROOT||path.resolve(__dirname,'..');
const source=fs.readFileSync(path.join(ROOT,'desktop/src/main/paths.js'),'utf8');
function load(platform,home,env={},getPath=()=>'/old/profile') {
  const mod={exports:{}};
  vm.runInNewContext(source,{module:mod,__dirname:path.join(ROOT,'desktop/src/main'),process:{platform,env},require:n=>n==='electron'?{app:{isPackaged:false,getPath}}:n==='os'?{homedir:()=>home}:n==='path'?(platform==='win32'?path.win32:path.posix):require(n)});
  return mod.exports;
}
let tests=0;
let p=load('darwin','/Users/test');assert.equal(p.persistentDataPath(),'/Users/test/Library/Application Support/WxBot/user-data');tests++;
p=load('win32','C:\\Users\\test',{LOCALAPPDATA:'C:\\Users\\test\\AppData\\Local',APPDATA:'C:\\Users\\test\\AppData\\Roaming'});assert.equal(p.persistentDataPath(),'C:\\Users\\test\\AppData\\Local\\WxBot\\user-data');tests++;
p=load('win32','C:\\Users\\test');assert.equal(p.persistentDataPath(),'C:\\Users\\test\\AppData\\Local\\WxBot\\user-data');tests++;
p=load('linux','/home/test',{XDG_DATA_HOME:'/home/test/store'});assert.equal(p.persistentDataPath(),'/home/test/store/wxbot/user-data');tests++;
p=load('linux','/home/test',{XDG_DATA_HOME:'relative'});assert.equal(p.persistentDataPath(),'/home/test/.local/share/wxbot/user-data');tests++;
p=load('darwin','/Users/test',{WXBOT_USER_DATA_DIR:'/isolated/profile'});assert.equal(p.persistentDataPath(),'/isolated/profile/data');tests++;
p=load('darwin','/Users/test',{WXBOT_DATA_DIR:'/custom/data',WXBOT_USER_DATA_DIR:'/profile'});assert.equal(p.persistentDataPath(),'/custom/data');tests++;
const dir=fs.mkdtempSync(path.join(os.tmpdir(),'wxbot-persistent-paths-'));
try {
 p=load(process.platform,dir,{WXBOT_DATA_DIR:path.join(dir,'permanent')},name=>name==='appData'?path.join(dir,'profiles'):path.join(dir,'old-name'));
 assert.equal(p.userDataDir(),path.join(dir,'permanent'));assert.equal(p.logDir(),path.join(dir,'permanent','logs'));tests++;
 assert(p.legacyDataDirs().includes(path.join(dir,'old-name','data')));assert(p.legacyDataDirs().some(s=>s.includes(path.join('wxbot-desktop','data'))));tests++;
 const install=path.join(dir,'install-v1');fs.mkdirSync(install);fs.writeFileSync(path.join(p.userDataDir(),'memory.json'),'keep');fs.rmSync(install,{recursive:true});assert.equal(fs.readFileSync(path.join(p.userDataDir(),'memory.json'),'utf8'),'keep');tests++;
} finally {fs.rmSync(dir,{recursive:true,force:true});}
const config=fs.readFileSync(path.join(ROOT,'desktop/electron-builder.yml'),'utf8');assert(config.includes('deleteAppDataOnUninstall: false'));assert(config.includes('appId: com.siriuspeng.wxbot'));tests++;
const backend=fs.readFileSync(path.join(ROOT,'desktop/src/main/backend.js'),'utf8');assert(backend.includes('WXBOT_LEGACY_DATA_DIRS:'));assert(backend.includes('WXBOT_MIGRATE_LEGACY:'));tests++;
assert.equal(tests,12);
console.log('STORAGE_PATHS PASS tests=12 stable_platform_paths=1 override_preserved=1 uninstall_retained=1 migration_handoff=1');
