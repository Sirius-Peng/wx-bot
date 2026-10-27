'use strict';
const {BrowserWindow,screen}=require('electron');
const fs=require('fs'),path=require('path'),P=require('./paths');
class CompanionWindow{
 constructor(){this.win=null;this.url='';this.timer=null;this.visibleRequested=false;this.file=path.join(P.userDataDir(),'companion-window.json');}
 _saveBounds(win){
  if(!win||win.isDestroyed())return;
  try{const temp=this.file+'.tmp';fs.mkdirSync(path.dirname(this.file),{recursive:true});fs.writeFileSync(temp,JSON.stringify(win.getBounds()));fs.renameSync(temp,this.file);}
  catch(error){console.warn('[companion] 保存窗口位置失败',error.message);}
 }
 show(url){
  const target=url||this.url;if(!/^http:\/\/127\.0\.0\.1:\d+$/.test(target))return;
  this.url=target;this.visibleRequested=true;
  if(this.win&&!this.win.isDestroyed()){this.win.setIgnoreMouseEvents(false);this.win.show();this.win.focus();return;}
  let bounds={width:380,height:620};try{bounds={...bounds,...JSON.parse(fs.readFileSync(this.file,'utf8'))};}catch(_){}
  bounds.width=Math.min(600,Math.max(340,bounds.width));bounds.height=Math.min(900,Math.max(420,bounds.height));
  const area=screen.getDisplayMatching({x:bounds.x||0,y:bounds.y||0,width:bounds.width,height:bounds.height}).workArea;
  bounds.x=Math.min(Math.max(bounds.x||area.x,area.x),area.x+Math.max(0,area.width-bounds.width));bounds.y=Math.min(Math.max(bounds.y||area.y,area.y),area.y+Math.max(0,area.height-bounds.height));
  const win=this.win=new BrowserWindow({...bounds,frame:false,transparent:true,alwaysOnTop:true,show:false,minWidth:340,minHeight:420,
   webPreferences:{preload:path.join(__dirname,'../preload/index.js'),contextIsolation:true,nodeIntegration:false,sandbox:false,spellcheck:false}});
  win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
  win.webContents.on('will-navigate',(event,target)=>{try{if(new URL(target).origin!==this.url)event.preventDefault();}catch(_){event.preventDefault();}});
  win.webContents.on('before-input-event',(event,input)=>{
   if(input.type!=='keyDown'||input.isComposing)return;
   if(input.key==='Escape'||((input.control||input.meta)&&input.key.toLowerCase()==='w')){event.preventDefault();this.close();}
  });
  win.once('ready-to-show',()=>{if(this.win===win&&!win.isDestroyed()&&this.visibleRequested)win.show();});
  // Closing the companion closes only this window, never the bot or its main UI.
  win.on('close',()=>{if(this.win===win){this.visibleRequested=false;clearTimeout(this.timer);this._saveBounds(win);}});
  const save=()=>{clearTimeout(this.timer);this.timer=setTimeout(()=>{if(this.win===win)this._saveBounds(win);},250);};
  win.on('move',save);win.on('resize',save);
  win.on('closed',()=>{if(this.win===win){clearTimeout(this.timer);this.win=null;this.visibleRequested=false;}});
  win.loadURL(this.url+'/companion');
 }
 hide(){this.visibleRequested=false;if(this.win&&!this.win.isDestroyed())this.win.hide();}
 close(){this.destroy();}
 reload(url){if(!/^http:\/\/127\.0\.0\.1:\d+$/.test(url))return;this.url=url;if(this.win&&!this.win.isDestroyed())this.win.loadURL(url+'/companion');}
 top(value){if(this.win&&!this.win.isDestroyed())this.win.setAlwaysOnTop(!!value);}
 through(value){if(this.win&&!this.win.isDestroyed())this.win.setIgnoreMouseEvents(!!value,{forward:true});}
 destroy(){this.visibleRequested=false;clearTimeout(this.timer);const win=this.win;if(win&&!win.isDestroyed()){this._saveBounds(win);win.destroy();}if(this.win===win)this.win=null;}
}
module.exports={CompanionWindow};
