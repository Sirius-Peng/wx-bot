"""Explicit, verified old-directory import, published before runtime construction.

A prepared ZIP is immutable: the old application can be removed after preparation.
The new application's latest data is backed up only after its writers have exited.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import uuid
import zipfile

from .storage import _atomic_json, _lock, _snapshot, verify_backup, MARKER, MANIFEST

IMPORT_PENDING = '.storage-import.pending.json'
IMPORT_REPORT = '.storage-import.json'
MAX_FILES = 30000
MAX_BYTES = 4 * 1024**3
_RESERVED = {MANIFEST, '.imports'}


def _internal(name):
    name = name.casefold()
    return name in {n.casefold() for n in _RESERVED} or name.startswith('.storage') or name.startswith('.migration-')


def _safe_name(name):
    if not isinstance(name, str): raise ValueError('备份路径格式无效')
    p = PurePosixPath(name)
    if p.as_posix() != name or name == '.' or not name or p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name:
        raise ValueError('目录包含不支持的文件路径')
    for part in p.parts:
        if any(ord(c)<32 or c in '<>\"|?*' for c in part) or part.endswith(('.', ' ')) or re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', part):
            raise ValueError('目录包含不兼容 Windows 的文件名')
    return p


def _walk(root, current=False):
    """Include unknown files, backups, logs and empty folders; exclude own controls."""
    directories = []
    files = []
    total = 0
    folded = set()
    def read_error(error): raise error
    for folder, dirs, names in os.walk(root, followlinks=False, onerror=read_error):
        rel = Path(folder).relative_to(root)
        if rel == Path('.'):
            dirs[:] = [d for d in dirs if not _internal(d) and not (current and d in {'.backups', 'logs'})]
            names = [n for n in names if not _internal(n)]
        for name in sorted(dirs + names):
            if rel == Path('.') and name.casefold() in {'logs', '.backups', 'config.json'} and name != name.casefold():
                raise ValueError('旧目录系统文件名大小写不规范，请使用 config.json、logs 和 .backups')
            path = Path(folder)/name
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError('目录含符号链接或特殊文件，请先整理为普通文件目录')
            relative = path.relative_to(root).as_posix()
            _safe_name(relative)
            if relative.casefold() in folded:
                raise ValueError('目录存在大小写冲突的文件名')
            folded.add(relative.casefold())
            if path.is_dir():
                directories.append(relative)
                if len(directories) > MAX_FILES: raise ValueError('导入目录超过 30000 个子目录，请先整理')
            else:
                total += path.stat().st_size
                files.append((path, relative))
                if len(files) > MAX_FILES or total > MAX_BYTES:
                    raise ValueError('导入目录超过 30000 个文件或 4 GiB，请先整理历史备份')
        dirs.sort()
    return files, sorted(directories), total


def _source(source, root):
    if not isinstance(source, str) or not source.strip():
        raise ValueError('请选择包含 config.json 的旧数据目录')
    path = Path(source).expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('请填写旧数据目录的绝对路径，不使用符号链接')
    path = path.resolve()
    if path == root or path.is_relative_to(root) or root.is_relative_to(path):
        raise ValueError('旧目录与当前目录必须互相独立')
    if not path.is_dir():
        raise ValueError('旧数据目录不存在')
    if not (path/'config.json').is_file():
        raise ValueError('所选目录没有 config.json；请选择旧版的数据目录，而非安装目录')
    return path


def _config(raw):
    from ..config_store import ConfigStore
    if len(raw) > 4 * 1024**2:
        raise ValueError('config.json 过大')
    try:
        cfg = json.loads(raw)
        if not isinstance(cfg, dict) or not cfg:
            raise ValueError()
        if any(key in cfg and not isinstance(cfg[key], dict) for key in ('bot', 'llm', 'adapter')):
            raise ValueError()
        ConfigStore.__new__(ConfigStore)._from_dict(json.loads(raw))
    except (ValueError, TypeError, AttributeError, KeyError):
        raise ValueError('旧 config.json 格式无效，请先检查旧版配置') from None
    return cfg


def _digest(files, directories):
    return hashlib.sha256(json.dumps({'files': files, 'directories': directories}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def inspect_import(source, data_dir):
    root = Path(data_dir).resolve()
    path = _source(source, root)
    files, directories, total = _walk(path)
    hashes = {}
    for p, name in files:
        before = p.stat()
        h = hashlib.sha256()
        with p.open('rb') as f:
            for chunk in iter(lambda: f.read(1024*1024), b''): h.update(chunk)
        after = p.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError('旧目录仍在写入，请退出旧版本后重新预览')
        hashes[name] = h.hexdigest()
    _config((path/'config.json').read_bytes())
    for p, name in files:
        if name in {'memory.json', 'onboarding.json', 'qq-bursts.json'} or (name.startswith('context-') and name.endswith('.json')) or (name.startswith('ilink/') and name.endswith('.json')):
            try:
                raw = json.loads(p.read_bytes())
                if name == 'memory.json':
                    from .memory import MemoryItem
                    if not isinstance(raw, dict) or not isinstance(raw.get('items', []), list): raise ValueError()
                    ids = set()
                    for entry in raw.get('items', []):
                        item = MemoryItem(**{k:v for k,v in entry.items() if k in MemoryItem.__dataclass_fields__})
                        if not isinstance(item.id, str) or not item.id or item.id in ids: raise ValueError()
                        ids.add(item.id)
            except (ValueError, UnicodeError, TypeError, AttributeError): raise ValueError('旧目录的状态 JSON 格式无效，请先检查旧数据') from None
        if name == 'admin-token':
            try: p.read_text(encoding='utf-8')
            except UnicodeError: raise ValueError('旧管理员 Token 文件编码无效') from None
    return {'source': str(path), 'files': len(files), 'bytes': total,
            'directories': len(directories), 'fingerprint': _digest(hashes, directories),
            'includes': {'persona': True, 'memory': 'memory.json' in hashes,
                         'messages': 'messages.jsonl' in hashes,
                         'wechat': any(n.startswith('ilink/') for n in hashes),
                         'contexts': any(n.startswith('context-') for n in hashes)},
            'message': '将替换当前配置与记忆，不混合两套身份；旧目录不修改。历史日志和备份完整保留在独立子目录，QQ Docker 卷不在此目录内。'}


def import_status(data_dir):
    root = Path(data_dir)
    for name in (IMPORT_PENDING, IMPORT_REPORT):
        if (root/name).is_file():
            report = json.loads((root/name).read_text(encoding='utf-8'))
            return {k: report[k] for k in ('status', 'source', 'files', 'backup', 'archive', 'id', 'message') if k in report}
    return {'status': 'none'}


def prepare_import(source, data_dir, fingerprint):
    root = Path(data_dir).resolve()
    path = _source(source, root)
    with _lock(root):
        if (root/IMPORT_PENDING).exists():
            raise ValueError('已有待导入数据，请重启后台完成或先取消')
        files, directories, _ = _walk(path)
        archive = _snapshot(path, root/'.backups', 'import', payload=lambda _: iter(files), directories=directories)
        try:
            manifest = verify_backup(archive)
            latest = inspect_import(str(path), root)
            if latest['fingerprint'] != fingerprint or _digest(manifest['files'], manifest.get('directories', [])) != fingerprint:
                raise ValueError('旧目录内容已变化，请退出旧版本后重新预览')
            with zipfile.ZipFile(archive) as z: _config(z.read('config.json'))
            aliases = [str(path)]
            try:
                previous = json.loads((path/MARKER).read_text(encoding='utf-8'))
                if previous.get('status') == 'migrated' and isinstance(previous.get('source'), str):
                    aliases.append(previous['source'])
            except (OSError, ValueError, AttributeError): pass
            report = {'version': 1, 'status': 'queued', 'aliases': aliases, 'id': uuid.uuid4().hex,
                      'source': str(path), 'files': len(manifest['files']), 'archive': archive.name,
                      'message': '旧数据已校验并准备好；重启后台后导入，导入前自动备份当前数据。'}
            _atomic_json(root/IMPORT_PENDING, report)
            return report
        except BaseException:
            archive.unlink(missing_ok=True)
            raise


def cancel_import(data_dir):
    root = Path(data_dir).resolve()
    with _lock(root):
        file = root/IMPORT_PENDING
        if file.is_file():
            pending = json.loads(file.read_text(encoding='utf-8'))
            if pending.get('status') != 'queued':
                raise ValueError('导入正在执行，请等待启动完成')
            file.unlink()  # Keep the verified source archive for later recovery.
    return {'ok': True, 'message': '已取消待导入任务，当前数据未改变。'}


def _archive(root, name, kind):
    if not re.fullmatch(r'wxbot-'+kind+r'-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}\.zip', name or ''):
        raise ValueError('导入备份名称无效')
    file = root/'.backups'/name
    if file.is_symlink() or (root/'.backups').is_symlink(): raise ValueError('备份路径含符号链接')
    manifest = verify_backup(file)
    for n in [*manifest['files'], *manifest.get('directories', [])]:
        _safe_name(n)
        if _internal(PurePosixPath(n).parts[0]): raise ValueError('备份包含内部控制文件')
    return file, manifest


def _target_name(name, id):
    if name.startswith('logs/') or name == 'logs': return 'logs/imported-'+id+name[4:]
    if name.startswith('.backups/') or name == '.backups': return '.backups/legacy-'+id+name[8:]
    return name


def _destination(root, name):
    _safe_name(name)
    target = root/name
    for parent in (target, *target.parents):
        if parent == root: break
        if parent.is_symlink(): raise ValueError('导入目标包含链接路径')
    if not target.resolve().is_relative_to(root): raise ValueError('导入目标超出数据目录')
    return target


def _write(root, name, content):
    target = _destination(root, name)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stream = content if hasattr(content, 'read') else io.BytesIO(content)
    digest = hashlib.sha256()
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix='.storage-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as out:
            for chunk in iter(lambda: stream.read(1024*1024), b''):
                out.write(chunk);digest.update(chunk)
            out.flush();os.fsync(out.fileno())
        os.chmod(tmp, 0o600);os.replace(tmp, target)
        check = hashlib.sha256()
        with target.open('rb') as f:
            for chunk in iter(lambda: f.read(1024*1024), b''): check.update(chunk)
        if check.digest() != digest.digest(): raise OSError('导入文件写入校验失败')
        return digest.hexdigest()
    finally: Path(tmp).unlink(missing_ok=True)


def _clear_active(root):
    files, dirs, _ = _walk(root, current=True)
    for p, _ in files: p.unlink()
    for name in sorted(dirs, key=lambda s: len(PurePosixPath(s).parts), reverse=True):
        (root/name).rmdir()


def _publish(root, archive, manifest, id=None, source=None):
    for name in sorted(manifest.get('directories', []), key=lambda s: len(PurePosixPath(s).parts)):
        name = _target_name(name, id) if id else name
        target = _destination(root, name)
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
    with zipfile.ZipFile(archive) as z:
        for name in manifest['files']:
            output = _target_name(name, id) if id else name
            if not (source and name == 'config.json'):
                with z.open(name) as stream: digest = _write(root, output, stream)
                if digest != manifest['files'][name]: raise OSError('导入文件校验失败')
                continue
            content = z.read(name)
            if source and name == 'config.json':
                cfg = _config(content)
                adapter = cfg.get('adapter', {})
                path = adapter.get('ilink_state_dir')
                for old in (source if isinstance(source, list) else [source]):
                    old = Path(old).expanduser().resolve()
                    candidate = Path(path).expanduser().resolve() if isinstance(path, str) and Path(path).is_absolute() else None
                    if candidate is not None and candidate.is_relative_to(old):
                        relative = candidate.relative_to(old)
                        # An older auto-migration alias is only valid when its state actually exists here.
                        if old == Path(source[0] if isinstance(source, list) else source).expanduser().resolve() or any(n.startswith(relative.as_posix()+'/') for n in manifest['files']):
                            adapter['ilink_state_dir'] = str(root/relative)
                            content = json.dumps(cfg, ensure_ascii=False, indent=2).encode()
                            break
            _write(root, output, content)


def _forget_markers(root):
    markers = []
    for name, field in (('memory-forgotten.json', 'markers'), ('memory.json', 'suppressions')):
        path = root/name
        if path.exists():
            raw = json.loads(path.read_text(encoding='utf-8'))
            value = raw.get(field, [])
            if not isinstance(value, list): raise ValueError('遗忘屏蔽记录格式无效')
            markers.extend(value)
    return {m['id']:m for m in markers if isinstance(m, dict) and m.get('id')}


def _merge_forgetting(root, retained):
    combined = {**_forget_markers(root), **{m['id']:m for m in retained}}
    if combined:
        _atomic_json(root/'memory-forgotten.json', {'version':1, 'markers':list(combined.values())})
        # Imported lossy summaries have no trustworthy map from prose to the
        # user's original evidence. They must not resurrect forgotten content.
        for path in root.glob('context-*.json'):
            raw = json.loads(path.read_text(encoding='utf-8'))
            raw['summaries'] = {}
            raw['summary_sources'] = {}
            _atomic_json(path, raw)
            from ..store_memory import MemoryStore
            MemoryStore(path=path).sync_suppressions(list(combined.values()))


def apply_pending_import(root):
    """Caller holds storage lock; executes before any stateful component is loaded."""
    root = Path(root).resolve()
    control = root/IMPORT_PENDING
    if not control.exists(): return None
    pending = json.loads(control.read_text(encoding='utf-8'))
    if not re.fullmatch('[a-f0-9]{32}', pending.get('id', '')): raise ValueError('导入任务标识无效')
    try:
        archive, manifest = _archive(root, pending['archive'], 'import')
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        if 'backup' in pending: raise  # An interrupted publication needs its verified archive.
        _atomic_json(root/IMPORT_REPORT, {**pending, 'status': 'failed', 'message': '待导入备份校验失败，当前数据未改变；请重新预览。'})
        control.unlink()
        return None
    if 'backup' not in pending:
        files, dirs, _ = _walk(root, current=True)
        backup = _snapshot(root, root/'.backups', 'preimport', payload=lambda _: iter(files), directories=dirs)
        pending.update(backup=backup.name, status='applying', forget_markers=list(_forget_markers(root).values()))
        _atomic_json(control, pending)  # Crash recovery always reuses the original backup.
    backup, original = _archive(root, pending['backup'], 'preimport')
    try:
        for name in [*manifest['files'], *manifest.get('directories', [])]:
            _destination(root, _target_name(name, pending['id']))
        _clear_active(root)
        _publish(root, archive, manifest, pending['id'], pending.get('aliases') or [pending['source']])
        _merge_forgetting(root, pending.get('forget_markers', []))
    except Exception:
        import shutil
        for base in (root/'logs'/('imported-'+pending['id']), root/'.backups'/('legacy-'+pending['id'])):
            if any(p.is_symlink() for p in (base, base.parent)): continue
            if base.is_dir(): shutil.rmtree(base)
            elif base.exists(): base.unlink()
        _clear_active(root)
        _publish(root, backup, original)
        report = {**pending, 'status': 'failed', 'message': '导入未完成，已恢复导入前数据；请检查空间和文件权限后重新预览。'}
        _atomic_json(root/IMPORT_REPORT, report);control.unlink()
        return None
    report = {**pending, 'status': 'imported', 'message': '旧目录已导入，当前数据的导入前备份和旧目录均保留。'}
    _atomic_json(root/MARKER, {'version': 1, 'status': 'migrated', 'source': pending['source'],
                             'archive': pending['archive'], 'imported_files': len(manifest['files'])})
    _atomic_json(root/IMPORT_REPORT, report)
    control.unlink()
    return report
