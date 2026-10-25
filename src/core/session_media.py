"""Immutable local image blobs for persisted conversation prefixes.

Only long inline image URLs are externalized. Provider requests must use thaw()
first; internal references are storage details, never replacement image inputs.
The context JSON path stays unchanged, and blobs are deliberately not collected:
forgetting session references and deleting physical backups are separate actions.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile


PREFIX = 'wxbot-session-media://'
INLINE_THRESHOLD = 4096
_REFERENCE = re.compile(r'wxbot-session-media://([0-9a-f]{64})\Z')


class SessionMedia:
    def __init__(self, path_to_context_json):
        self.root = self.directory = None
        if path_to_context_json is None:
            return
        context = Path(path_to_context_json)
        if context.name in ('', '.', '..'):
            raise ValueError('Invalid session context filename')
        # The user-selected parent is the trust root. Resolve its legitimate
        # aliases once (including macOS /var), without following blob symlinks.
        self.root = context.parent.resolve()
        self.directory = self.root/(context.stem+'-media')

    @staticmethod
    def _urls(session):
        messages = session.get('messages')
        if not isinstance(messages, list):
            return
        for message in messages:
            content = message.get('content') if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get('type') != 'image_url':
                    continue
                image = part.get('image_url')
                if isinstance(image, dict) and isinstance(image.get('url'), str):
                    yield image

    @staticmethod
    def _reference(value):
        match = _REFERENCE.fullmatch(value)
        if match is None:
            raise ValueError('Invalid session media reference')
        return match.group(1)

    @staticmethod
    def _is_internal(value):
        return value.lower().startswith('wxbot-session-media:')

    @staticmethod
    def _entry(path, *, directory=False):
        metadata = path.lstat()
        if (stat.S_ISLNK(metadata.st_mode)
                or (getattr(path, 'is_junction', lambda: False)())):
            raise ValueError('Session media links are not allowed')
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected(metadata.st_mode):
            raise ValueError('Invalid session media resource type')
        return metadata

    def _directory(self, *, create=False):
        # A later redirect of the fixed root must not send a blob write elsewhere.
        if self.root.resolve() != self.root:
            raise ValueError('Session media root changed')
        if create:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._entry(self.root, directory=True)
        try:
            self._entry(self.directory, directory=True)
        except FileNotFoundError:
            if not create:
                raise
            self.directory.mkdir(mode=0o700)
            self._entry(self.directory, directory=True)
        return self.directory

    def _read(self, digest):
        if not isinstance(digest,str) or re.fullmatch(r'[0-9a-f]{64}',digest) is None:
            raise ValueError('Invalid session media digest')
        path = self._directory()/(digest+'.blob')
        before = self._entry(path)
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, 'rb') as source:
            opened = os.fstat(source.fileno())
            if (not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
                raise ValueError('Session media resource changed')
            data = source.read()
        after = self._entry(path)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise ValueError('Session media resource changed')
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Session media checksum mismatch')
        value = data.decode('utf-8')
        if not value.startswith('data:') or len(value) <= INLINE_THRESHOLD:
            raise ValueError('Invalid session media payload')
        return value

    def _write(self, value):
        data = value.encode('utf-8')
        digest = hashlib.sha256(data).hexdigest()
        path = self._directory(create=True)/(digest+'.blob')
        try:
            self._entry(path)
        except FileNotFoundError:
            pass
        else:
            if self._read(digest) != value:
                raise ValueError('Session media content mismatch')
            return PREFIX+digest
        descriptor, temporary = tempfile.mkstemp(dir=self.directory, prefix='.blob-', suffix='.tmp')
        temporary = Path(temporary)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, 'wb') as output:
                descriptor = None
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            self._directory()
            # A second writer may have published the same immutable blob while
            # this one flushed. Never replace an already valid resource.
            try:
                self._entry(path)
            except FileNotFoundError:
                os.replace(temporary, path)
            else:
                if self._read(digest) != value:
                    raise ValueError('Session media content mismatch')
            return PREFIX+digest
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def freeze(self, session):
        if not isinstance(session, dict):
            raise ValueError('Session must be an object')
        result = deepcopy(session)
        if self.directory is None:
            return result
        frozen = {}
        for image in self._urls(result):
            value = image['url']
            if value.startswith('data:') and len(value) > INLINE_THRESHOLD:
                if value not in frozen:
                    frozen[value] = self._write(value)
                image['url'] = frozen[value]
            elif self._is_internal(value):
                if value not in frozen:
                    self._read(self._reference(value))
                    frozen[value] = value
        return result

    def thaw(self, session):
        if not isinstance(session, dict):
            raise ValueError('Session must be an object')
        result = deepcopy(session)
        if self.directory is None:
            return result
        resolved = {}
        for image in self._urls(result):
            value = image['url']
            if self._is_internal(value):
                if value not in resolved:
                    resolved[value] = self._read(self._reference(value))
                image['url'] = resolved[value]
        return result
