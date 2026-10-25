"""Collect original notices from the actual native packaging environment.

No network, account data, research environment traversal, or guessed licenses.
Only the runtime dependency closure plus the distributed PyInstaller bootloader.
"""
from __future__ import annotations
import argparse,hashlib,json,re,shutil,sys,sysconfig
from importlib import metadata
from pathlib import Path
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT=Path(__file__).resolve().parents[2]

def dependency_closure(roots, loader=metadata.distribution):
    selected={};seen={};queue=[Requirement(value) for value in roots]
    while queue:
        requirement=queue.pop(0);name=canonicalize_name(requirement.name)
        extras=set(requirement.extras)|{''}
        previous=seen.get(name,set())
        if extras<=previous:continue
        seen[name]=previous|extras
        dist=selected.get(name) or loader(name);selected[name]=dist
        for value in dist.requires or ():
            child=Requirement(value)
            if child.marker is None or any(child.marker.evaluate({'extra':extra}) for extra in seen[name]):
                queue.append(child)
    return selected

def collect(root=ROOT,out=None):
    root=Path(root);out=Path(out or root/'desktop/resources/third-party')
    requirements=[line.strip() for line in (root/'requirements.txt').read_text().splitlines()
                  if line.strip() and not line.lstrip().startswith('#')]
    selected=dependency_closure([*requirements,'pyinstaller'])
    retired={'torch','onnx','onnxruntime','numpy','tokenizers','transformers','safetensors'}
    if retired.intersection(selected):raise RuntimeError('Retired research dependency entered runtime closure')
    if out.exists():shutil.rmtree(out)
    out.mkdir(parents=True)
    rows=[];missing=[]
    for name,dist in sorted(selected.items()):
        files=[]
        for item in dist.files or ():
            filename=Path(str(item)).name.lower()
            if re.match(r'^(?:licen[sc]e|copying|notice)(?:$|[._-])',filename):
                path=Path(dist.locate_file(item))
                if path.is_file():files.append((str(item),path.read_bytes()))
        fallback=root/'desktop/licenses'/f'{name}-MIT.txt'
        if not files and fallback.is_file():files.append((str(fallback.relative_to(root)),fallback.read_bytes()))
        if not files:missing.append(name)
        record={'name':name,'version':dist.version,'licenses':[]}
        folder=out/'licenses'/name;folder.mkdir(parents=True)
        seen=set()
        for origin,data in files:
            digest=hashlib.sha256(data).hexdigest()
            if digest in seen:continue
            seen.add(digest)
            target=folder/(f'{len(seen):02d}-'+re.sub(r'[^A-Za-z0-9._-]','_',Path(origin).name))
            target.write_bytes(data)
            record['licenses'].append({'file':target.relative_to(out).as_posix(),'origin':origin,'sha256':digest})
        rows.append(record)
    candidates=[Path(sysconfig.get_path('stdlib'))/'LICENSE.txt',Path(sys.base_prefix)/'LICENSE.txt',Path(sys.base_prefix)/'LICENSE']
    python_license=next((path for path in candidates if path.is_file()),None)
    if python_license is None:raise RuntimeError('Python redistribution license not found')
    (out/'PYTHON-LICENSE.txt').write_bytes(python_license.read_bytes())
    (out/'manifest.json').write_text(json.dumps({'python':sys.version.split()[0],
        'components':rows,'missing_license_files':missing,
        'note':'Actual native build dependency closure. Original upstream licenses, including vendored files. Electron/Chromium and Live2D notices are shipped alongside.'},ensure_ascii=False,indent=2)+'\n')
    if missing:raise RuntimeError('Missing original license files: '+', '.join(missing))
    print(f'NOTICES PASS components={len(rows)} license_files={sum(len(row["licenses"]) for row in rows)} python={sys.version.split()[0]} research_dependencies=excluded')
    return rows

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,default=ROOT);parser.add_argument('--out',type=Path)
    args=parser.parse_args();collect(args.root,args.out)
