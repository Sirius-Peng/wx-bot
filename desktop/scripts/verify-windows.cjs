'use strict';
const fs = require('fs'), path = require('path'), assert = require('assert');
function peMachine(filename) {
 const b=fs.readFileSync(filename);
 assert(b.length>64 && b.toString('ascii',0,2)==='MZ', 'Not a Windows PE executable: '+filename);
 const offset=b.readUInt32LE(0x3c);
 assert(offset+26<=b.length && b.toString('binary',offset,offset+4)==='PE\0\0', 'Invalid PE header');
 return b.readUInt16LE(offset+4);
}
function verify(root, backendOnly=false) {
 const desktop=path.join(root,'desktop'), version=JSON.parse(fs.readFileSync(path.join(desktop,'package.json'),'utf8')).version;
 const backend=path.join(desktop,'resources','backend','wxbot-server.exe');
 assert.strictEqual(peMachine(backend),0x8664,'Backend is not native x64');
 assert(!fs.existsSync(path.join(desktop,'resources','backend','wxbot-server')),'Mac/Linux sidecar mixed into Windows bundle');
 if(backendOnly)return 'WINDOWS_PE PASS: backend=x64';
 const unpacked=path.join(desktop,'dist','win-unpacked');
 assert.strictEqual(peMachine(path.join(unpacked,'微信AI助手.exe')),0x8664,'Electron is not x64');
 const bundled=path.join(unpacked,'resources','backend','wxbot-server.exe');
 assert.strictEqual(peMachine(bundled),0x8664);
 assert(fs.readFileSync(bundled).equals(fs.readFileSync(backend)),'Bundled backend differs');
 const asar=require(path.join(desktop,'node_modules','@electron','asar'));
 const pkg=JSON.parse(asar.extractFile(path.join(unpacked,'resources','app.asar'),'package.json').toString('utf8'));
 assert.strictEqual(pkg.version,version);
 for(const name of ['Setup','Portable']) {
  const file=path.join(desktop,'dist',`WxBot-${version}-Windows-x64-${name}.exe`);
  assert([0x14c,0x8664].includes(peMachine(file)),'Invalid Windows bootstrap executable');
 }
 return `WINDOWS_PACKAGE PASS: version=${version}; Electron=x64; backend=x64; NSIS=PE; portable=PE; ASAR=MATCH`;
}
if(require.main===module)console.log(verify(path.resolve(__dirname,'..','..'),process.argv.includes('--backend-only')));
module.exports={peMachine,verify};
