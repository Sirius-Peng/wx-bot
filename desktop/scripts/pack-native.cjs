'use strict';
const {spawnSync}=require('child_process');
const script=process.platform==='win32'?'pack:win':process.platform==='darwin'?'pack:mac':null;
if(!script)throw new Error('Use native macOS or Windows to build the desktop package.');
const result=process.platform==='win32'
 ? spawnSync(process.env.ComSpec||'cmd.exe',['/d','/s','/c',`npm run ${script}`],{stdio:'inherit'})
 : spawnSync('npm',['run',script],{stdio:'inherit'});
if(result.error)throw result.error;
process.exit(result.status===null?1:result.status);
