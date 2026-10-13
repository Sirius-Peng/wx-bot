'use strict';
const {spawn}=require('child_process');
const electron=require('electron');
const child=spawn(electron,['.'],{stdio:'inherit',windowsHide:true,env:{...process.env,WXBOT_DEV:'1'}});
child.on('error',e=>{console.error(e.message);process.exitCode=1;});
child.on('exit',code=>{process.exitCode=code===null?1:code;});
