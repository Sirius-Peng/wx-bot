// Independent controller over the official Cubism Framework, no upstream pet UI.
import {CubismFramework} from '@cubism/live2dcubismframework';
import {CubismUserModel} from '@cubism/model/cubismusermodel';
import {CubismMatrix44} from '@cubism/math/cubismmatrix44';

class Character extends CubismUserModel {
  idle:any=null;
  tick(dt:number,t:number,state:string){
    this._model.loadParameters();
    if(this.idle){
      if(this._motionManager.isFinished())this._motionManager.startMotionPriority(this.idle,false,1);
      this._motionManager.updateMotion(this._model,dt);
    }
    const ids=CubismFramework.getIdManager();
    const set=(name:string,value:number)=>this._model.setParameterValueById(ids.getId(name),value);
    set('ParamBreath',(Math.sin(t*1.4)+1)*.4);
    const blink=Math.pow(Math.max(0,Math.cos(t*1.1)),48);
    set('ParamEyeLOpen',1-blink);set('ParamEyeROpen',1-blink);
    set('ParamAngleZ',Math.sin(t*.6)*(state==='thinking'?4:1.5));
    this._model.saveParameters();
    if(this._physics)this._physics.evaluate(this._model,dt);
    if(this._pose)this._pose.updateParameters(this._model,dt);
    this._model.update();
  }
}
let started=false;
export class Renderer {
  canvas:HTMLCanvasElement;model:any;gl:any;textures:any[]=[];frame=0;generation=0;state='idle';last=0;
  onError:(error:Error)=>void=()=>{};
  constructor(canvas:HTMLCanvasElement){this.canvas=canvas;}
  async load(url:string){
    this.dispose();const generation=++this.generation;
    if(!started){CubismFramework.startUp();CubismFramework.initialize();started=true;}
    const gl=this.canvas.getContext('webgl',{alpha:true,premultipliedAlpha:true});
    if(!gl)throw Error('WebGL不可用');this.gl=gl;
    const response=await fetch(url);if(!response.ok)throw Error('模型清单读取失败');
    const json=await response.json(),refs=json.FileReferences;
    const buffer=async(file:string)=>{const r=await fetch(new URL(file,url));if(!r.ok)throw Error('模型资源缺失');return r.arrayBuffer();};
    const model=new Character();this.model=model;model.loadModel(await buffer(refs.Moc),true);
    if(refs.Physics){const b=await buffer(refs.Physics);model.loadPhysics(b,b.byteLength);}
    if(refs.Pose){const b=await buffer(refs.Pose);model.loadPose(b,b.byteLength);}
    const idle=(refs.Motions?.Idle||refs.Motions?.idle||[])[0];
    if(idle){const b=await buffer(idle.File);model.idle=model.loadMotion(b,b.byteLength,'idle');model.idle.setLoop(true);model.idle.setEffectIds([],[]);}
    this.resize();model.createRenderer(this.canvas.width,this.canvas.height);
    const renderer=model.getRenderer();renderer.startUp(gl);renderer.loadShaders('/static/live2d/Shaders/WebGL/');renderer.setIsPremultipliedAlpha(true);
    for(let i=0;i<refs.Textures.length;i++){
      const image=await new Promise<HTMLImageElement>((resolve,reject)=>{const img=new Image();img.onload=()=>resolve(img);img.onerror=()=>reject(Error('纹理读取失败'));img.src=new URL(refs.Textures[i],url).href;});
      if(generation!==this.generation){model.release();return;}
      const texture=gl.createTexture();this.textures.push(texture);gl.bindTexture(gl.TEXTURE_2D,texture);
      gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL,1);gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,image);
      gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);renderer.bindTexture(i,texture);
    }
    model.getModelMatrix().setHeight(1.85);this.resume();
  }
  resize(){const ratio=Math.min(2,window.devicePixelRatio||1);this.canvas.width=Math.max(1,Math.round(this.canvas.clientWidth*ratio));this.canvas.height=Math.max(1,Math.round(this.canvas.clientHeight*ratio));
    this.model?.getRenderer()?.setRenderTargetSize(this.canvas.width,this.canvas.height);}
  resume(){
    if(this.frame||!this.model)return;this.last=performance.now();
    const draw=(now:number)=>{
      this.frame=0;if(document.hidden||!this.model)return;
      try{
      const dt=Math.min(.05,(now-this.last)/1000);this.last=now;const gl=this.gl;
      this.model.tick(dt,now/1000,this.state);gl.viewport(0,0,this.canvas.width,this.canvas.height);gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
      const matrix=new CubismMatrix44();matrix.scale(this.canvas.height/this.canvas.width,1);matrix.multiplyByMatrix(this.model.getModelMatrix());
      const renderer=this.model.getRenderer();renderer.setMvpMatrix(matrix);renderer.setRenderState(null,[0,0,this.canvas.width,this.canvas.height]);renderer.drawModel();
      this.frame=requestAnimationFrame(draw);
      }catch(error){this.dispose();this.onError(error instanceof Error?error:Error(String(error)));}
    };this.frame=requestAnimationFrame(draw);
  }
  pause(){cancelAnimationFrame(this.frame);this.frame=0;}
  dispose(){this.pause();++this.generation;if(this.model){this.model.release();this.model=null;}if(this.gl)for(const texture of this.textures)this.gl.deleteTexture(texture);this.textures=[];}
}
