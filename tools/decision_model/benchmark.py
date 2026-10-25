"""Research-only CPU benchmark. Does not accept a model for application release.

Reports the exact graph input length, including boundary tokens, and keeps the
quality-gate status separate from speed. No user input or network is used.
"""
from __future__ import annotations
import argparse,json,os,platform,sys,time
try:import resource
except ImportError:resource=None
from pathlib import Path
from src.core.local_decision_worker import DecisionModel


def _windows_peak_rss_bytes():
    """Read the process peak working set on Windows without a new runtime dependency."""
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD),
            ('PeakWorkingSetSize',ctypes.c_size_t),('WorkingSetSize',ctypes.c_size_t),
            ('QuotaPeakPagedPoolUsage',ctypes.c_size_t),('QuotaPagedPoolUsage',ctypes.c_size_t),
            ('QuotaPeakNonPagedPoolUsage',ctypes.c_size_t),('QuotaNonPagedPoolUsage',ctypes.c_size_t),
            ('PagefileUsage',ctypes.c_size_t),('PeakPagefileUsage',ctypes.c_size_t)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    psapi=ctypes.WinDLL('psapi',use_last_error=True)
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype=wintypes.BOOL
    counters=Counters();counters.cb=ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(counters),counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(counters.PeakWorkingSetSize)


def peak_rss_bytes():
    if sys.platform=='win32':
        try:return _windows_peak_rss_bytes()
        except (OSError,AttributeError):return None
    if resource is None:return None
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024))


def runtime_metadata(model):
    """Report the loaded policy without guessing an ORT build's spin default."""
    return {'onnxruntime_version':getattr(model,'onnxruntime_version',None),
            'runtime_cpu_affinity':getattr(model,'runtime_cpu_affinity',None),
            'runtime_policy':{
                'threads':model.policy.get('threads',2),
                'denormal_as_zero':model.policy.get('denormal_as_zero',False),
                'prefer_performance_cores':model.policy.get('prefer_performance_cores',False),
                'intra_op_allow_spinning':model.policy.get('intra_op_allow_spinning'),
                'intra_op_spinning_config_override':'0' if model.policy.get('intra_op_allow_spinning') is False else None,
                'spinning_default':'ORT build default when no override; not inferred as enabled'}}


def benchmark(root,repeats=100):
    if isinstance(repeats,bool) or not isinstance(repeats,int) or repeats<1:raise ValueError('invalid_repeats')
    started=time.monotonic();model=DecisionModel(root,require_gate=False)
    cold_ms=(time.monotonic()-started)*1000
    np=model.np
    text=('今天下班后我想和你聊聊音乐、生活，还有周末一起散步的计划。'
          'This is a local Chinese conversation timing fixture, not a user message. ')*30
    tokenize=[]
    for _ in range(10):
        t=time.monotonic();tokens=model.tokenizer.encode(text,add_special_tokens=False).ids
        tokenize.append((time.monotonic()-t)*1000)
    rows=[]
    for length in (32,128,256):
        ids=[model.policy['special_tokens']['cls']]+tokens[:length-2]+[model.policy['special_tokens']['sep']]
        assert len(ids)==length
        values={'input_ids':np.asarray([ids],dtype=np.int64),'attention_mask':np.ones((1,length),dtype=np.int64),
                'marker_pos':np.zeros((1,3),dtype=np.int64),'marker_mask':np.ones((1,3),dtype=bool),'qtype':np.zeros((1,),dtype=np.int64)}
        feed={k:v for k,v in values.items() if k in model._inputs}
        for _ in range(5):model.session.run(None,feed)
        timings=[]
        for _ in range(repeats):
            t=time.monotonic();model.session.run(None,feed);timings.append((time.monotonic()-t)*1000)
        rows.append({'input_tokens':length,'samples':repeats,'p50_ms':sorted(timings)[len(timings)//2],
                     'p95_ms':sorted(timings)[int(.95*(len(timings)-1))]})
    manifest=json.loads((Path(root)/'manifest.json').read_text())
    return {**runtime_metadata(model),'platform':platform.platform(),'machine':platform.machine(),'processor':platform.processor(),
            'logical_cpus':os.cpu_count(),'providers':model.session.get_providers(),
            'threads':model.policy.get('threads'),'cold_hash_load_ms':cold_ms,
            'process_peak_rss_bytes':peak_rss_bytes(),
            'package_bytes':sum(p.stat().st_size for p in Path(root).rglob('*') if p.is_file()),
            'tokenize_long_fixture_p95_ms':max(tokenize),'benchmarks':rows,
            'quality_gate':manifest['quality_gate'],'release_accepted':False,
            'note':'Research measurement on the reported platform only; does not bypass the quality gate. Peak RSS includes model loading.'}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--model-dir',required=True);p.add_argument('--output',required=True);p.add_argument('--repeats',type=int,default=100);a=p.parse_args()
    result=benchmark(a.model_dir,a.repeats);Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print(json.dumps(result,ensure_ascii=False))
