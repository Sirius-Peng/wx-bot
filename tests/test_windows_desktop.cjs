'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const ROOT=process.env.WXBOT_TEST_ROOT||path.resolve(__dirname,'..');
const source=fs.readFileSync(path.join(ROOT,'desktop/src/main/backend.js'),'utf8');
const calls=[];const mod={exports:{}};
vm.runInNewContext(source,{require:n=>n==='./paths'?{}:n==='child_process'?{spawn(){},spawnSync(...args){calls.push(args);return {status:0};}}:require(n),module:mod,process:{platform:'win32',env:{}},setTimeout,clearTimeout},{filename:'backend.js'});
const b=new mod.exports.Backend();b._killTree({pid:4321});assert.strictEqual(calls[0][0],'taskkill.exe');assert.deepStrictEqual(Array.from(calls[0][1]),['/PID','4321','/T','/F']);assert.strictEqual(calls[0][2].windowsHide,true);
const {peMachine}=require(path.join(ROOT,'desktop/scripts/verify-windows.cjs'));const os=require('os');const dir=fs.mkdtempSync(path.join(os.tmpdir(),'win-pe-test-'));
try {const f=path.join(dir,'native.exe'),buffer=Buffer.alloc(128);buffer.write('MZ');buffer.writeUInt32LE(64,0x3c);buffer.write('PE\0\0',64,'binary');buffer.writeUInt16LE(0x8664,68);fs.writeFileSync(f,buffer);assert.strictEqual(peMachine(f),0x8664);fs.writeFileSync(f,'not-PE');assert.throws(()=>peMachine(f));}finally{fs.rmSync(dir,{recursive:true,force:true});}
console.log('WINDOWS_DESKTOP PASS: PID-only tree cleanup; no console; PE-x64 validated; nonPE rejected');
