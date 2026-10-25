'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
let assertions=0;const check=(value,message)=>{assert.ok(value,message);assertions++;};
class Element{
  constructor(){this.children=[];this.listeners={};this.textContent='';this.value='';this.scrollHeight=0;this.style={};const classes=new Set();this.classList={toggle:(name,on)=>on?classes.add(name):classes.delete(name),contains:name=>classes.has(name)};}
  addEventListener(name,fn){this.listeners[name]=fn;}
  appendChild(node){this.children.push(node);node.parent=this;}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);}
}
const elements=new Map();const el=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const sockets=[];class Socket{
  static OPEN=1;
  constructor(){this.readyState=1;this.sent=[];sockets.push(this);}
  send(value){this.sent.push(JSON.parse(value));}
  close(){}
}
let uuid=0;
const context=vm.createContext({console,Map,Set,URL,location:{protocol:'http:',host:'localhost',href:'http://localhost/companion'},
  document:{getElementById:el,createElement:()=>new Element(),addEventListener(){},hidden:false},
  window:{addEventListener(){}},WebSocket:Socket,crypto:{randomUUID:()=>`uuid-${++uuid}`},
  setTimeout:()=>1,clearTimeout(){},
  fetch:async()=>({ok:true,json:async()=>({models:[],model_id:'',persona_space:'space-a'})})});
const evaluate=code=>vm.runInContext(code,context);
(async()=>{
  evaluate(fs.readFileSync(path.join(__dirname,'../src/web/companion.js'),'utf8'));
  await new Promise(resolve=>setImmediate(resolve));
  let socket=sockets[0];check(!!socket,'initial status opens socket');socket.onopen();
  check(socket.sent[0].persona_space==='space-a','sync announces current persona');
  context.receive({type:'accepted',seq:1,client_id:'a-user',text:'A原话',persona_space:'space-a'});
  context.receive({type:'delta',seq:2,turn_id:'a-reply',text:'A草稿',persona_space:'space-a'});
  check(el('conversation').children.length===2,'old space displays one user and draft');
  socket.readyState=0;el('text').value='旧断线消息';el('chat').listeners.submit({preventDefault(){}});
  check(evaluate('pending.size')===1,'offline unsent input stays pending');
  socket.readyState=1;context.receive({type:'space_changed',seq:3,persona_space:'space-b'});
  check(evaluate('personaSpace')==='space-b','space change selects B');
  check(el('conversation').children.length===0,'space change clears all old bubbles');
  check(evaluate('pending.size')===0,'old unacknowledged inputs are not replayed into B');
  check(socket.sent.at(-1).type==='sync'&&socket.sent.at(-1).persona_space==='space-b','switch requests B snapshot');
  context.receive({type:'completed',seq:4,turn_id:'a-reply',text:'迟到A答案',persona_space:'space-a'});
  context.receive({type:'state',seq:5,state:'thinking',persona_space:'space-a'});
  context.receive({type:'error',message:'旧空间错误',persona_space:'space-a'});
  check(el('conversation').children.length===0,'late completed A is invisible in B');
  check(el('state').textContent==='在这里','old typing cannot change B state');
  check(el('error').textContent==='','old errors do not contaminate current space');
  context.receive({type:'accepted',seq:8,client_id:'b-user',text:'B原话',persona_space:'space-b'});
  context.receive({type:'delta',seq:9,turn_id:'b-reply',text:'B',persona_space:'space-b'});
  context.receive({type:'delta',seq:9,turn_id:'b-reply',text:'B',persona_space:'space-b'});
  check(evaluate('bubbles.get("b-reply").textContent')==='B','duplicate seq cannot append a draft twice');
  context.receive({type:'completed',seq:10,turn_id:'b-reply',text:'完整B回答',persona_space:'space-b'});
  check(evaluate('bubbles.get("b-reply").textContent')==='完整B回答','final replaces one draft');
  check(!evaluate('bubbles.get("b-reply").classList.contains("draft")'),'final is no longer draft');
  context.receive({type:'accepted',seq:6,client_id:'b-history',text:'B历史',persona_space:'space-b'});
  check(evaluate('bubbles.has("user:b-history")'),'snapshot history is accepted even after newer live seq');
  el('text').value='新空间消息';el('chat').listeners.submit({preventDefault(){}});
  check(socket.sent.at(-1).persona_space==='space-b','new input is bound to current space');
  context.connect();socket=sockets.at(-1);socket.onopen();
  check(socket.sent.every(value=>value.persona_space==='space-b'),'reconnect sends only B payloads');
  check(socket.sent.filter(value=>value.type==='message').length===1,'reconnect retries only current unacknowledged input');
  check(evaluate('!bubbles.has("a-reply")'),'reconnect does not restore old drafts');
  console.log(`COMPANION_SPACES PASS assertions=${assertions}`);
})().catch(error=>{console.error(error);process.exitCode=1;});
