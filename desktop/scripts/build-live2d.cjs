'use strict';
// Official SDK is fetched only by the developer build, never on user startup.
const fs=require('fs'),path=require('path'),os=require('os'),crypto=require('crypto');
const cache=path.join(os.homedir(),'.cache','wxbot-live2d-v3');
const sdk=process.env.WXBOT_CUBISM_SDK||path.join(cache,'sdk','CubismSdkForWeb-5-r.5');
if(!fs.existsSync(sdk))throw new Error('Set WXBOT_CUBISM_SDK to official CubismSdkForWeb-5-r.5');
const out=path.resolve(__dirname,'../../src/web/live2d');fs.mkdirSync(out,{recursive:true});
fs.cpSync(path.join(sdk,'Framework','Shaders'),path.join(out,'Shaders'),{recursive:true});
fs.copyFileSync(path.join(sdk,'Core','live2dcubismcore.min.js'),path.join(out,'live2dcubismcore.min.js'));
for(const [from,to] of [['LICENSE.md','SDK-LICENSE.md'],['Core/LICENSE.md','CORE-LICENSE.md'],['Framework/LICENSE.md','FRAMEWORK-LICENSE.md']])fs.copyFileSync(path.join(sdk,from),path.join(out,to));
require('esbuild').buildSync({entryPoints:[path.resolve(__dirname,'../../src/web/companion-renderer.ts')],bundle:true,minify:true,format:'iife',globalName:'WxBotLive2D',
 outfile:path.join(out,'renderer.js'),alias:{'@cubism':path.join(sdk,'Framework','src')}});
const hashes={};for(const file of ['renderer.js','live2dcubismcore.min.js'])hashes[file]=crypto.createHash('sha256').update(fs.readFileSync(path.join(out,file))).digest('hex');
fs.writeFileSync(path.join(out,'manifest.json'),JSON.stringify({sdk:'5-r.5',core:'06.00.0001',source:'https://cubism.live2d.com/sdk-web/bin/CubismSdkForWeb-5-r.5.zip',files:hashes},null,2));
console.log('LIVE2D_BUNDLE PASS');
