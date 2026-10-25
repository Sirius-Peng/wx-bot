'use strict';
const {BrowserWindow,screen,app}=require('electron');
const fs=require('fs'),path=require('path'),P=require('./paths');
class CompanionWindow{
 constructor(){this.win=null;this.url='';this.timer=null;this.file=path.join(P.userDataDir(),'companion-window.json');}
 show(url){
  this.url=url||this.url;if(!/^http:\/\/127\.0\.0\.1:\d+$/.test(this.url))return;
  if(this.win&&!this.win.isDestroyed()){this.win.setIgnoreMouseEvents(false);this.win.show();this.win.focus();return;}
  let bounds={width:380,height:620};try{bounds={...bounds,...JSON.parse(fs.readFileSync(this.file,'utf8'))};}catch(_){}
  bounds.width=Math.min(600,Math.max(340,bounds.width));bounds.height=Math.min(900,Math.max(420,bounds.height));
  const area=screen.getDisplayMatching({x:bounds.x||0,y:bounds.y||0,width:bounds.width,height:bounds.height}).workArea;
  bounds.x=Math.min(Math.max(bounds.x||area.x,area.x),area.x+Math.max(0,area.width-bounds.width));bounds.y=Math.min(Math.max(bounds.y||area.y,area.y),area.y+Math.max(0,area.height-bounds.height));
  this.win=new BrowserWindow({...bounds,frame:false,transparent:true,alwaysOnTop:true,show:false,minWidth:340,minHeight:420,
   webPreferences:{preload:path.join(__dirname,'../preload/index.js'),contextIsolation:true,nodeIntegration:false,sandbox:false,spellcheck:false}});
  this.win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
  this.win.webContents.on('will-navigate',(event,target)=>{if(new URL(target).origin!==this.url)event.preventDefault();});
  this.win.loadURL(this.url+'/companion');this.win.once('ready-to-show',()=>{this.win?.show();});
  this.win.on('close',event=>{if(!app.isQuitting){event.preventDefault();this.win.hide();}});
  const save=()=>{clearTimeout(this.timer);this.timer=setTimeout(()=>{if(!this.win||this.win.isDestroyed())return;const temp=this.file+'.tmp';fs.mkdirSync(path.dirname(this.file),{recursive:true});fs.writeFileSync(temp,JSON.stringify(this.win.getBounds()));fs.renameSync(temp,this.file);},250);};
  this.win.on('move',save);this.win.on('resize',save);this.win.on('closed',()=>{this.win=null;});
 }
 hide(){this.win?.hide();}
 reload(url){this.url=url;if(this.win&&!this.win.isDestroyed())this.win.loadURL(url+'/companion');}
 top(value){this.win?.setAlwaysOnTop(!!value);}
 through(value){this.win?.setIgnoreMouseEvents(!!value,{forward:true});}
 destroy(){clearTimeout(this.timer);this.win?.destroy();this.win=null;}
}
module.exports={CompanionWindow};
