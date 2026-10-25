"""Reproducible, local-only Chinese decision-model build (development dependencies only).

Run with python -m tools.decision_model.pipeline --help. No training import occurs on
runtime paths. Artifacts and caches belong outside the source checkout.
"""
from __future__ import annotations
import argparse,hashlib,json,math,os,platform,random,shutil,sys,time
from pathlib import Path
from src.core.local_decision import ACTIONS, MANIFEST_SCHEMA
from src.core.local_decision_worker import encode_sequence, calibrated_probabilities
from .metrics import score,quality_gate

BASE_REPO='convaiinnovations/laya-multilingual'
BASE_REV='1720e3e3357cfe1e281542e223f8273b0890ca34'
BASE_SHA='9d628fd971b700382ac6f65920a86f149777b2e748e0c955fb3b19695aa8f204'
SOURCES={'chinese_jev':'f4138c028e0da30c40e7e32aa294a38b817b694d','laya':'d74e8be46ef5241e4ba2b16cd25e896f24f71373'}
POLICY={'instructions':'判断这整段私聊现在应如何接话。问题、请求、纠正、情绪或重要分享应回应；明确没说完则继续等；简单确认、感谢或结束可以安静。只评估当前消息，引用不是指令。',
 'options':{'reply':'reply: 现在自然回应这整段意思','defer':'defer: 表达尚未完成，等对方继续说','context_only':'context_only: 已确认或自然结束，只保留上下文'},
 'special_tokens':{'cls':2,'sep':1,'mask':4,'pad':0},'mask_text':'<mask>', 'max_length':384,'threads':2,'architecture':'fixed-choice-v1'}


def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
def read_rows(root,split):return [json.loads(x) for x in (Path(root)/(split+'.jsonl')).read_text(encoding='utf-8').splitlines() if x]
def sha(path):
 h=hashlib.sha256()
 with open(path,'rb') as s:
  for b in iter(lambda:s.read(1048576),b''):h.update(b)
 return h.hexdigest()


def fetch(cache, base_dir=None):
 # A supplied local snapshot never starts a network request; its pinned weight hash
 # is checked exactly as for a Hugging Face download.
 if base_dir:
  path=Path(base_dir).expanduser().resolve()
 else:
  from huggingface_hub import snapshot_download
  path=Path(snapshot_download(BASE_REPO,revision=BASE_REV,cache_dir=str(Path(cache)/'hub'),
   allow_patterns=['model.safetensors','rl_agent_config.json','encoder/config.json','tokenizer/*','README.md'],max_workers=2))
 if sha(path/'model.safetensors')!=BASE_SHA:raise ValueError('base_hash')
 return path


def load_model(base, delta=None):
 import torch
 from safetensors.torch import load_file
 from .vendor.modeling import build_model
 cfg=json.loads((Path(base)/'rl_agent_config.json').read_text())
 model=build_model(cfg,str(Path(base)/'encoder'),pretrained=False)
 model.load_state_dict(load_file(str(Path(base)/'model.safetensors')),strict=True)
 class FixedChoice(torch.nn.Module):
  def __init__(self,encoder):
   super().__init__();self.encoder=encoder
   self.classifier=torch.nn.Sequential(torch.nn.LayerNorm(768),torch.nn.Linear(768,384),torch.nn.GELU(),torch.nn.Dropout(.1),torch.nn.Linear(384,3))
  def forward(self,input_ids,attention_mask,marker_pos=None,marker_mask=None,qtype=None,detach_encoder=False):
   hidden=self.encoder(input_ids=input_ids,attention_mask=attention_mask).last_hidden_state
   if detach_encoder:hidden=hidden.detach()
   pooled=(hidden*attention_mask[:,:,None]).sum(1)/attention_mask.sum(1,keepdim=True).clamp(min=1)
   logits=self.classifier(pooled).float()
   return logits,logits
 model=FixedChoice(model.encoder)
 if delta:
  resume_info=resume_metadata(delta)
  tensors=load_file(str(delta))
  validate_resume_tensors(tensors,model.state_dict())
  model.load_state_dict(tensors,strict=False)
  model._resume_info=resume_info

 return model


def batch(rows,tokenizer,device,order=None):
 import torch
 encoded=[];labels=[]
 for row in rows:
  permutation=list(range(3)) if order is None or POLICY.get('architecture')=='fixed-choice-v1' else list(order)
  ids,markers=encode_sequence(tokenizer,row['state'],POLICY,order=permutation)
  encoded.append((ids,markers));labels.append(permutation.index(ACTIONS.index(row['label'])))
 width=max(len(x[0]) for x in encoded)
 ids=torch.zeros((len(rows),width),dtype=torch.long);att=torch.zeros_like(ids)
 for i,(token_ids,_) in enumerate(encoded):ids[i,:len(token_ids)]=torch.tensor(token_ids);att[i,:len(token_ids)]=1
 inputs=[ids,att,torch.tensor([x[1] for x in encoded]),torch.ones((len(rows),3),dtype=torch.bool),torch.zeros(len(rows),dtype=torch.long)]
 return [x.to(device) for x in inputs],torch.tensor(labels,device=device)


def infer(model,rows,tokenizer,device,bs=8,order=None):
 import torch
 output=[];model.eval()
 with torch.no_grad():
  for i in range(0,len(rows),bs):
   inputs,_=batch(rows[i:i+bs],tokenizer,device,order)
   logits=model(*inputs)[0].cpu().tolist()
   if order and POLICY.get('architecture')!='fixed-choice-v1':
    logits=[[values[order.index(j)] for j in range(3)] for values in logits]
   output+=logits
 return output


def trainable_key(name):
 return name.startswith('classifier.') or any(name.startswith('encoder.layers.'+str(i)+'.') for i in (18,19,20,21))


def resume_metadata(checkpoint):
 """Validate provenance before reading weights. Legacy architecture is tensor-verified.

 Resume is deliberately weights-only with a fresh optimizer; it is not presented
 as a byte-identical continuation of an interrupted optimizer/RNG state.
 """
 path=Path(checkpoint).expanduser().resolve()
 if not path.is_file():raise ValueError('resume_checkpoint_missing')
 metadata_path=path.with_name('training.json')
 if not metadata_path.is_file():raise ValueError('resume_training_metadata_missing')
 meta=json.loads(metadata_path.read_text(encoding='utf-8'))
 expected={'base':BASE_REPO,'revision':BASE_REV,'base_sha256':BASE_SHA,'sources':SOURCES}
 for key,value in expected.items():
  if meta.get(key)!=value:raise ValueError('resume_'+key+'_mismatch')
 checkpoint_sha=sha(path)
 if meta.get('checkpoint_sha256') not in (None,checkpoint_sha):raise ValueError('resume_checkpoint_hash_mismatch')
 architecture=meta.get('architecture')
 if architecture not in (None,POLICY['architecture']):raise ValueError('resume_architecture_mismatch')
 return {'path':str(path),'sha256':checkpoint_sha,'metadata_sha256':sha(metadata_path),
  'mode':'weights_only_new_optimizer','source_epochs':len(meta.get('epochs',[])),
  'legacy_architecture_metadata':architecture is None,'checkpoint_metadata_hash_bound':'checkpoint_sha256' in meta,
  'architecture_validation':'exact classifier and final-four encoder keys/shapes',
  'source_training_description':meta.get('training')}


def validate_resume_tensors(tensors,expected):
 required={k for k in expected if trainable_key(k)}
 if not required or set(tensors)!=required:raise ValueError('resume_tensor_keys_mismatch')
 for key,value in tensors.items():
  if tuple(value.shape)!=tuple(expected[key].shape):raise ValueError('resume_tensor_shape_mismatch:'+key)


def select_device(torch,requested='auto'):
 cuda=bool(torch.cuda.is_available())
 mps_backend=getattr(torch.backends,'mps',None)
 mps=bool(mps_backend and mps_backend.is_available())
 if requested=='auto':return 'cuda' if cuda else ('mps' if mps else 'cpu')
 if requested not in ('cuda','cpu','mps'):raise ValueError('invalid_device')
 if requested=='cuda' and not cuda:raise ValueError('cuda_not_available')
 if requested=='mps' and not mps:raise ValueError('mps_not_available')
 return requested


def configure_training(torch,args):
 requested=getattr(args,'device','auto');device=select_device(torch,requested)
 threads=getattr(args,'threads',0)
 if threads<0:raise ValueError('invalid_threads')
 threads=threads or max(1,min(4,os.cpu_count() or 1))
 random.seed(args.seed);torch.manual_seed(args.seed);torch.set_num_threads(threads)
 if torch.cuda.is_available():torch.cuda.manual_seed_all(args.seed)
 deterministic=getattr(args,'deterministic',True)
 torch.use_deterministic_algorithms(deterministic)
 if hasattr(torch.backends,'cudnn'):
  torch.backends.cudnn.deterministic=deterministic;torch.backends.cudnn.benchmark=False
 if device=='cuda' and deterministic:
  # CUDA flash/memory-efficient SDPA backward is not deterministic. The math
  # backend keeps the seed contract explicit rather than merely issuing warnings.
  torch.backends.cuda.enable_flash_sdp(False);torch.backends.cuda.enable_mem_efficient_sdp(False)
 result={'device':device,'requested_device':requested,'torch_version':torch.__version__,
  'python':sys.version.split()[0],'platform':platform.platform(),'machine':platform.machine(),
  'logical_cpus':os.cpu_count(),'threads':threads,'seed':args.seed,
  'deterministic_algorithms':deterministic,'cuda_build':torch.version.cuda,
  'gpu':None,'reproducibility':'same source/data/checkpoint/seed and compatible software/hardware; not cross-device bit identity'}
 if device=='cuda':
  props=torch.cuda.get_device_properties(torch.cuda.current_device())
  result['gpu']={'name':props.name,'total_memory_bytes':props.total_memory,'capability':list(torch.cuda.get_device_capability())}
 return result


def validate_train_args(args):
 if args.epochs<1 or args.batch<1 or not math.isfinite(args.lr) or args.lr<=0 or (args.limit is not None and args.limit<1):raise ValueError('invalid_training_arguments')
 if not args.data:raise ValueError('training_data_required')
 out=Path(args.out)
 if any((out/name).exists() for name in ('task-head.safetensors','training.json','checkpoints')):
  raise ValueError('training_output_exists_choose_new_run')
 if getattr(args,'resume',None):resume_metadata(args.resume)


def train(args):
 validate_train_args(args)
 # Set before importing CUDA libraries, so deterministic cuBLAS is effective.
 if getattr(args,'deterministic',True):os.environ.setdefault('CUBLAS_WORKSPACE_CONFIG',':4096:8')
 import torch
 from tokenizers import Tokenizer
 from safetensors.torch import save_file
 environment=configure_training(torch,args);device=environment['device']
 base=fetch(args.cache,getattr(args,'base_dir',None));out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
 model=load_model(base,getattr(args,'resume',None))
 for name,p in model.named_parameters():p.requires_grad_(trainable_key(name))
 # Lower encoder layers stay frozen; the last four and the fixed typed head train.
 model=model.to(device);tokenizer=Tokenizer.from_file(str(base/'tokenizer/tokenizer.json'))
 rows=read_rows(args.data,'train');cal=read_rows(args.data,'calibration')
 dataset=json.loads((Path(args.data)/'dataset.json').read_text(encoding='utf-8'))
 split_hashes={split:sha(Path(args.data)/(split+'.jsonl')) for split in ('train','calibration')}
 for split,value in split_hashes.items():
  recorded=dataset.get('splits',{}).get(split,{}).get('sha256')
  if recorded!=value:raise ValueError('training_'+split+'_split_changed')
 if args.limit:rows=rows[:args.limit]
 if not rows or not cal:raise ValueError('empty_training_or_calibration_split')
 trainable=sum(p.numel() for p in model.parameters() if p.requires_grad)
 optimizer=torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],lr=args.lr,weight_decay=.01)
 log=[];started=time.monotonic();resume=getattr(model,'_resume_info',None)
 metadata={'base':BASE_REPO,'revision':BASE_REV,'base_sha256':BASE_SHA,'sources':SOURCES,
  'training':'frozen lower encoder; fitted final four encoder layers and fixed three-class decision head; AI-authored synthetic',
  'architecture':POLICY['architecture'],'trainable_parameters':trainable,'device':device,
  'environment':environment,'seed':args.seed,'rows':len(rows),'batch':args.batch,'learning_rate':args.lr,
  'dataset':dataset,'split_sha256':split_hashes,'resume':resume,'epochs':log,
  'source_file_sha256':{name:sha(Path(__file__).parent/name) for name in ('pipeline.py','vendor/modeling.py')}}
 print('TRAIN_ENVIRONMENT '+json.dumps(environment,ensure_ascii=False),flush=True)
 print('TRAIN_RUN '+json.dumps({'out':str(out),'resume':resume,'epochs_additional':args.epochs,'batch':args.batch,'rows':len(rows)}),flush=True)
 for epoch in range(args.epochs):
  random.shuffle(rows);model.train();model.encoder.eval();losses=[]
  for i in range(0,len(rows),args.batch):
   order=random.sample(range(3),3)
   inputs,labels=batch(rows[i:i+args.batch],tokenizer,device,order)
   optimizer.zero_grad(set_to_none=True)
   logits=model(*inputs,detach_encoder=False)[0]
   loss=torch.nn.functional.cross_entropy(logits,labels);loss.backward()
   torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad],1.)
   optimizer.step();losses.append(float(loss.detach().cpu()))
   if (i//args.batch)%25==0:print(json.dumps({'epoch':epoch+1,'step':i//args.batch,'loss':losses[-1],'seconds':time.monotonic()-started,'device':device}),flush=True)
  entry={'epoch':epoch+1,'loss':sum(losses)/len(losses),'seconds':time.monotonic()-started}
  if device=='cuda':entry['gpu_peak_allocated_bytes']=torch.cuda.max_memory_allocated()
  log.append(entry)
  trained={k for k,p in model.named_parameters() if p.requires_grad}
  delta={k:v.detach().cpu().contiguous() for k,v in model.state_dict().items() if k in trained}
  checkpoint_dir=out/'checkpoints'/('epoch-%04d'%(epoch+1));checkpoint_dir.mkdir(parents=True,exist_ok=False)
  checkpoint=checkpoint_dir/'task-head.safetensors'
  save_file(delta,str(checkpoint),metadata={'architecture':POLICY['architecture'],'base_sha256':BASE_SHA,'base_revision':BASE_REV})
  metadata['checkpoint_sha256']=sha(checkpoint)
  write(checkpoint_dir/'training.json',metadata)
  # Root is a convenience copy for the existing calibration/export CLI. Each
  # immutable epoch checkpoint (and the original resume run) remains intact.
  shutil.copyfile(checkpoint,out/'task-head.safetensors.tmp');os.replace(out/'task-head.safetensors.tmp',out/'task-head.safetensors')
  write(out/'training.json',metadata)
  print('EPOCH_SAVED '+json.dumps({'epoch':epoch+1,'path':str(checkpoint),'sha256':sha(checkpoint)}),flush=True)
 logits=infer(model,cal,tokenizer,device,bs=args.batch)
 write(out/'calibration-logits.json',{'split_sha256':split_hashes['calibration'],'logits':logits})
 print('TRAIN_COMPLETE',out,flush=True)


def fit_calibration(rows,logits):
 import numpy as np
 from scipy.optimize import minimize
 x=np.asarray(logits,dtype='float64');y=np.asarray([ACTIONS.index(r['label']) for r in rows])
 def nll(z):
  v=x/np.exp(z[0]);v-=v.max(axis=1,keepdims=True)
  return float(np.mean(-v[np.arange(len(y)),y]+np.log(np.exp(v).sum(axis=1))))
 result=minimize(nll,[0.],bounds=[(-2.3,2.3)],method='L-BFGS-B')
 calibration={'temperature':float(np.exp(result.x[0])),'bias':[0.,0.,0.],'min_margin':.2,
   'fitted_on_independent_split':True,'thresholds':{a:.85 for a in ACTIONS},'fit_records':len(rows)}
 probs=[calibrated_probabilities(v,calibration) for v in logits]
 # Silence is harder to undo than an extra friendly reply: choose a per-class selective threshold.
 for action in ('defer','context_only'):
  threshold=1.000001
  for t in [i/1000 for i in range(850,1001)]:
   selected=[(r,p) for r,p in zip(rows,probs) if max(p,key=p.get)==action and p[action]>=t and sorted(p.values(),reverse=True)[0]-sorted(p.values(),reverse=True)[1]>=.2]
   if len(selected)>=30 and sum(r['label']==action for r,_ in selected)/len(selected)>=.995:
    threshold=t;break
  calibration['thresholds'][action]=threshold
 return calibration


def calibrate(args):
 root=Path(args.out);rows=read_rows(args.data,'calibration');blob=json.loads((root/'calibration-logits.json').read_text())
 if blob['split_sha256']!=sha(Path(args.data)/'calibration.jsonl'):raise ValueError('calibration_split_changed')
 calibration=fit_calibration(rows,blob['logits']);calibration['split_sha256']=blob['split_sha256']
 write(root/'calibration.json',calibration);print(json.dumps(calibration),flush=True)


def export_training_provenance(root):
 # Only actual training records bind release independence; never infer missing
 # history from the new evaluation corpus or a legacy descriptive string.
 root=Path(root);metadata_path=root/'training.json'
 training=json.loads(metadata_path.read_text(encoding='utf-8'))
 splits=training.get('split_sha256')
 valid=isinstance(splits,dict) and all(isinstance(splits.get(k),str) and len(splits[k])==64
  and all(c in '0123456789abcdef' for c in splits[k]) for k in ('train','calibration'))
 return {'schema':'wxbot.decision-training-provenance.v1',
  'training_metadata_sha256':sha(metadata_path),'checkpoint_sha256':sha(root/'task-head.safetensors'),
  'split_sha256':splits if isinstance(splits,dict) else {},
  'history_complete':bool(valid and training.get('resume') is None),
  'history_note':'Resumed or unbound legacy training history requires separate evidence; no independence inferred.'}


def export(args):
 import torch,onnx
 from tokenizers import Tokenizer
 from onnxruntime.quantization import quantize_dynamic,QuantType
 base=fetch(args.cache,getattr(args,'base_dir',None));root=Path(args.out);package=root/'package';package.mkdir(exist_ok=True)
 model=load_model(base,root/'task-head.safetensors').eval().cpu()
 # torch's legacy MultiheadAttention export bakes the tracing sequence length
 # into a reshape. Use identical explicit Q/K/V math with symbolic B/L axes.
 class DynamicAttention(torch.nn.Module):
  def __init__(self,original):
   super().__init__();self.in_proj_weight=original.in_proj_weight;self.in_proj_bias=original.in_proj_bias
   self.out_proj=original.out_proj;self.num_heads=original.num_heads;self.embed_dim=original.embed_dim
   self.batch_first=True;self._qkv_same_embed_dim=True
  def forward(self,query,key,value,key_padding_mask=None,need_weights=False,attn_mask=None,is_causal=False):
   b,l,d=query.shape;h=self.num_heads;size=self.embed_dim//h
   q,k,v=torch.nn.functional.linear(query,self.in_proj_weight,self.in_proj_bias).chunk(3,dim=-1)
   q=q.reshape(b,l,h,size).transpose(1,2);k=k.reshape(b,l,h,size).transpose(1,2);v=v.reshape(b,l,h,size).transpose(1,2)
   scores=(q@k.transpose(-2,-1))/(size**.5)
   if key_padding_mask is not None:
    if key_padding_mask.dtype==torch.bool:scores=scores.masked_fill(key_padding_mask[:,None,None,:],float('-inf'))
    else:scores=scores+key_padding_mask[:,None,None,:]
   result=(torch.softmax(scores,dim=-1)@v).transpose(1,2).reshape(b,l,d)
   return self.out_proj(result),None
 if hasattr(model,'head'):
  for layer in model.head.layers:layer.self_attn=DynamicAttention(layer.self_attn)
 class Graph(torch.nn.Module):
  def __init__(self,inner):super().__init__();self.inner=inner
  def forward(self,input_ids,attention_mask,marker_pos,marker_mask,qtype):return self.inner(input_ids,attention_mask,marker_pos,marker_mask,qtype)[0]
 tokenizer=Tokenizer.from_file(str(base/'tokenizer/tokenizer.json'))
 rows=read_rows(args.data,'calibration')[:2];inputs,_=batch(rows,tokenizer,'cpu')
 torch.set_num_threads(4)
 torch.backends.mha.set_fastpath_enabled(False)
 # Reference outputs and quantized outputs must both pass independent batch/length probes.
 with torch.no_grad():
  torch.onnx.export(Graph(model),tuple(inputs),str(root/'fp32.onnx'),opset_version=18,dynamo=False,
   input_names=['input_ids','attention_mask','marker_pos','marker_mask','qtype'],output_names=['logits'],
   dynamic_axes={k:{0:'batch',1:'sequence'} for k in ('input_ids','attention_mask')}|{'marker_pos':{0:'batch'},'marker_mask':{0:'batch'},'qtype':{0:'batch'},'logits':{0:'batch'}})
 quantize_dynamic(str(root/'fp32.onnx'),str(package/'model.onnx'),op_types_to_quantize=['MatMul'],weight_type=QuantType.QInt8,per_channel=False)
 shutil.copyfile(base/'tokenizer/tokenizer.json',package/'tokenizer.json');write(package/'policy.json',POLICY)
 shutil.copyfile(Path(__file__).parent/'vendor/LICENSE',package/'LICENSE')
 shutil.copyfile(Path(__file__).parent/'vendor/NOTICE',package/'NOTICE')
 shutil.copyfile(base/'README.md',package/'BASE_MODEL_CARD.md')
 # Refit the quantized graph, not the float graph's probability distribution.
 write(package/'calibration.json',json.loads((root/'calibration.json').read_text()))
 manifest={'schema':MANIFEST_SCHEMA,'actions':list(ACTIONS),'model_id':'wxbot-decision-zh-v1','base':BASE_REPO,'base_revision':BASE_REV,
   'source_revisions':SOURCES,'training_provenance':export_training_provenance(root),'calibration':{'fitted_on_independent_split':True},'quality_gate':{'passed':False,'reason':'not_evaluated'},'files':{}}
 for f in package.iterdir():
  if f.name!='manifest.json' and f.is_file():manifest['files'][f.name]=sha(f)
 write(package/'manifest.json',manifest)
 from src.core.local_decision_worker import DecisionModel
 runtime=DecisionModel(package,require_gate=False);cal=read_rows(args.data,'calibration');logits=[runtime.logits(r['state']) for r in cal]
 fitted=fit_calibration(cal,logits);fitted['split_sha256']=sha(Path(args.data)/'calibration.jsonl');fitted['quantized']=True
 write(package/'calibration.json',fitted);manifest['files']['calibration.json']=sha(package/'calibration.json');write(package/'manifest.json',manifest)
 probes=[]
 for n in (1,2,3):
  ins,_=batch(read_rows(args.data,'calibration')[:n],tokenizer,'cpu')
  import onnxruntime as ort,numpy as np
  session=ort.InferenceSession(str(package/'model.onnx'),providers=['CPUExecutionProvider'])
  keys=['input_ids','attention_mask','marker_pos','marker_mask','qtype'];feed={k:v.numpy() for k,v in zip(keys,ins) if k in {i.name for i in session.get_inputs()}}
  with torch.no_grad():reference=Graph(model)(*ins).numpy()
  actual=session.run(None,feed)[0];probes.append({'batch':n,'max_logit_delta':float(np.max(np.abs(reference-actual)))})
 write(root/'export.json',{'opset':18,'quantization':'MatMul dynamic per-tensor QInt8','model_bytes':(package/'model.onnx').stat().st_size,'batch_probes':probes})
 print('EXPORT_COMPLETE',package,flush=True)


def evaluate(args):
 from src.core.local_decision_worker import DecisionModel
 root=Path(args.out);package=root/'package';runtime=DecisionModel(package,require_gate=False)
 rows=read_rows(args.data,'test');raw=[];times=[];flips=0
 # An old synthetic test used during development is not a blind release set.
 # A truthy flag alone cannot assert session independence: bind all split bytes,
 # explicit frozen/unused declarations, disjoint actual session ids and states.
 dataset=json.loads((Path(args.data)/'dataset.json').read_text(encoding='utf-8'))
 eligibility_reasons=[]
 manifest=json.loads((package/'manifest.json').read_text(encoding='utf-8'))
 provenance=manifest.get('training_provenance',{})
 if not isinstance(provenance,dict):provenance={}
 def digest(value):return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)
 bound=provenance.get('split_sha256',{})
 if (provenance.get('schema')!='wxbot.decision-training-provenance.v1'
     or not digest(provenance.get('training_metadata_sha256')) or not digest(provenance.get('checkpoint_sha256'))
     or not isinstance(bound,dict) or bound.get('train')!=sha(Path(args.data)/'train.jsonl')
     or bound.get('calibration')!=sha(Path(args.data)/'calibration.jsonl')):
  eligibility_reasons.append('actual_training_splits_unbound')
 if provenance.get('history_complete') is not True:eligibility_reasons.append('training_history_unverified')
 if runtime.calibration.get('split_sha256')!=sha(Path(args.data)/'calibration.jsonl'):
  eligibility_reasons.append('actual_calibration_split_mismatch')
 if dataset.get('release_acceptance_eligible') is not True:eligibility_reasons.append('release_acceptance_not_declared')
 if dataset.get('evaluation_role')!='locked_blind_acceptance':eligibility_reasons.append('not_locked_blind_acceptance')
 if dataset.get('split_unit')!='independent_session':eligibility_reasons.append('split_unit_not_independent_session')
 if dataset.get('frozen_before_training') is not True:eligibility_reasons.append('not_frozen_before_training')
 split_metadata=dataset.get('splits',{})
 if not isinstance(split_metadata,dict):split_metadata={};eligibility_reasons.append('invalid_split_metadata')
 test_metadata=split_metadata.get('test',{})
 if not isinstance(test_metadata,dict):test_metadata={}
 if test_metadata.get('role')!='locked_blind_acceptance' or test_metadata.get('used_for_development') is not False:
  eligibility_reasons.append('test_not_unused_locked_acceptance')
 sessions={};states={}
 for split in ('train','calibration','test'):
  actual=rows if split=='test' else read_rows(args.data,split)
  declared=split_metadata.get(split,{})
  if not isinstance(declared,dict):declared={}
  declared_sessions=declared.get('session_ids')
  actual_sessions={r.get('session_id') for r in actual if isinstance(r.get('session_id'),str) and r['session_id'].strip()}
  if (not actual or any(not isinstance(r.get('session_id'),str) or not r['session_id'].strip() for r in actual)
      or not isinstance(declared_sessions,list) or any(not isinstance(s,str) or not s.strip() for s in declared_sessions)
      or len(set(declared_sessions))!=len(declared_sessions) or actual_sessions!=set(declared_sessions)):
   eligibility_reasons.append(split+'_invalid_or_unbound_sessions')
  if declared.get('sha256')!=sha(Path(args.data)/(split+'.jsonl')) or declared.get('records')!=len(actual):
   eligibility_reasons.append(split+'_hash_or_record_mismatch')
  sessions[split]=actual_sessions
  states[split]={json.dumps(r.get('state'),sort_keys=True,ensure_ascii=False,separators=(',',':')) for r in actual}
 for left,right in (('train','calibration'),('train','test'),('calibration','test')):
  if sessions[left]&sessions[right]:eligibility_reasons.append(left+'_'+right+'_session_overlap')
  if states[left]&states[right]:eligibility_reasons.append(left+'_'+right+'_state_overlap')
 independent=not eligibility_reasons
 runtime.logits({'current':'你好'})
 for i,row in enumerate(rows):
  start=time.monotonic();logits=runtime.logits(row['state']);times.append((time.monotonic()-start)*1000)
  raw.append(calibrated_probabilities(logits,runtime.calibration))
  if i<150:
   swapped=runtime.logits(row['state'],[2,1,0]);flips+=max(range(3),key=logits.__getitem__)!=max(range(3),key=swapped.__getitem__)
 metrics=score(rows,raw,runtime.calibration['thresholds'],runtime.calibration['min_margin'])
 from .benchmark import benchmark
 cpu=benchmark(package)
 p95=next(r['p95_ms'] for r in cpu['benchmarks'] if r['input_tokens']==256)
 flip=flips/min(150,len(rows));gate=quality_gate(metrics,latency_p95_ms=p95,flips=flip,independent=independent)
 cpu.pop('quality_gate',None)  # Speed evidence must not embed a previous evaluation's gate.
 report={'metrics':metrics,'quality_gate':gate,'warm_p95_ms':p95,'option_order_flip_rate':flip,
   'platform':__import__('platform').platform(),'split_sha256':sha(Path(args.data)/'test.jsonl'),
   'benchmark':cpu,'test_fixture_warm_p95_ms':sorted(times)[int(.95*(len(times)-1))],
   'dataset_sha256':sha(Path(args.data)/'dataset.json'),
   'release_acceptance_eligible':independent,'acceptance_ineligibility_reasons':eligibility_reasons,
   'provenance':('Declared frozen, unused, session-disjoint blind acceptance; this declaration does not imply human annotation.' if independent else
     'Development regression only, NOT blind release acceptance; existing synthetic/regression tests cannot clear the independent_split release gate.'),
   'dataset_provenance':dataset.get('provenance','unknown')}
 write(root/'evaluation.json',report)
 write(root/'predictions.json', [{'id':r['id'],'label':r['label'],'probabilities':p} for r,p in zip(rows,raw)])
 manifest=json.loads((package/'manifest.json').read_text());manifest['quality_gate']=gate;manifest['evaluation']=report
 write(package/'manifest.json',manifest);print(json.dumps(report,ensure_ascii=False),flush=True)
 return 0 if gate['passed'] else 2


def main():
 p=argparse.ArgumentParser();p.add_argument('command',choices=['fetch','prepare','train','calibrate','export','evaluate','package-check'])
 p.add_argument('--cache',default=str(Path.home()/'.cache/wxbot-decision-v3'));p.add_argument('--out',required=True);p.add_argument('--data')
 p.add_argument('--epochs',type=int,default=2);p.add_argument('--batch',type=int,default=8);p.add_argument('--lr',type=float,default=2e-5)
 p.add_argument('--limit',type=int);p.add_argument('--seed',type=int,default=215)
 p.add_argument('--device',choices=['auto','cpu','cuda','mps'],default='auto')
 p.add_argument('--resume',help='task-head.safetensors with adjacent training.json; weights-only, new optimizer')
 p.add_argument('--base-dir',help='local pinned base snapshot, verified without any download')
 p.add_argument('--threads',type=int,default=0,help='CPU threads; 0 uses at most four logical CPUs')
 p.add_argument('--deterministic',action=argparse.BooleanOptionalAction,default=True)
 a=p.parse_args()
 if a.command=='fetch':print(fetch(a.cache,a.base_dir))
 elif a.command=='prepare':
  from .corpus import prepare
  print(json.dumps(prepare(a.out),ensure_ascii=False))
 elif a.command=='train':train(a)
 elif a.command=='calibrate':calibrate(a)
 elif a.command=='export':export(a)
 elif a.command=='evaluate':return evaluate(a)
 else:
  from src.core.local_decision import load_manifest
  load_manifest(a.out);print('LOCAL_DECISION_PACKAGE PASS')
 return 0
if __name__=='__main__':raise SystemExit(main())
