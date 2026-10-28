"""Own a newly created native gateway tree, never adopt an existing QQ/Node PID.

Windows starts suspended, assigns an unnamed non-inheritable kill-on-close Job,
then resumes the first thread. No child can execute outside that Job in between.
POSIX creates a fresh session. The direct child is kept waitable until its owned
process group has been signalled, preventing PID/process-group ID reuse.

Output is continuously drained from one bounded PIPE by a daemon reader. UTF-8
records are filtered before any bytes reach the private log. Long records are
omitted whole rather than risking a secret split over chunk boundaries. No shell
is involved; the caller supplies the filter and owns log rotation.
"""
from __future__ import annotations

import asyncio
import codecs
import atexit
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import threading
from typing import Callable

_WINDOWS = os.name == 'nt'
_OWNED: set = set()


def _validate(argv, cwd, env, log_path):
    if (not isinstance(argv, list) or not argv or
            any(not isinstance(arg, str) or '\0' in arg for arg in argv)):
        raise ValueError('Native command must be a nonempty string argument list')
    executable = Path(argv[0])
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError('Native executable must be an existing absolute file')
    cwd = Path(cwd).resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError('Native working directory does not exist')
    if not isinstance(env, dict) or any(
            not isinstance(k, str) or not k or '=' in k or '\0' in k or
            not isinstance(v, str) or '\0' in v for k, v in env.items()):
        raise ValueError('Native environment contains an invalid entry')
    log_path = Path(log_path).absolute()
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if log_path.is_symlink():
        raise ValueError('Native log symlinks are not accepted')
    return list(argv), cwd, dict(env), log_path


def _open_log(path):
    # Windows CRT text mode expands LF to CRLF, breaking byte caps and hashes.
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    try:
        if os.name != 'nt':
            os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


class _LogPump:
    """Bounded raw PIPE -> complete UTF-8 record -> redactor -> private file.

    No raw output is ever written first. A record over 16 KiB is discarded whole;
    buffering a suffix and writing earlier chunks would leak split credentials.
    A full log or a failed redactor still drains output, never blocks a gateway.
    """
    MAX_LINE_CHARS = 16 * 1024
    MAX_LOG_BYTES = 8 * 1024 * 1024

    def __init__(self, path, output_filter):
        self._log_fd = _open_log(path)
        self._written = os.fstat(self._log_fd).st_size
        self._filter = output_filter or (lambda line: line)
        self._decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self._buffer = ''; self._discard = False; self._disabled = False
        self.error = ''; self._limit_noted = False
        self._stop = threading.Event(); self._read_fd = self.child_fd = None
        try:
            self._read_fd, self.child_fd = os.pipe()
            if os.name == 'nt':
                import ctypes
                from ctypes import wintypes
                import msvcrt
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                self._peek = kernel.PeekNamedPipe
                self._peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                      wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
                self._peek.restype = wintypes.BOOL
                self._read_handle = msvcrt.get_osfhandle(self._read_fd)
                self._ctypes, self._wintypes = ctypes, wintypes
            else:
                os.set_blocking(self._read_fd, False)
            self.thread = threading.Thread(target=self._run, name='native-output', daemon=True)
            self.thread.start()
        except BaseException:
            for fd in (self._read_fd, self.child_fd, self._log_fd):
                if fd is not None:os.close(fd)
            raise

    def close_writer(self):
        if self.child_fd is not None:
            os.close(self.child_fd); self.child_fd = None

    def _write(self, text):
        if self._disabled:return
        data = text.encode('utf-8', errors='replace')
        if self._written + len(data) > self.MAX_LOG_BYTES:
            if self._limit_noted:return
            self._limit_noted = True
            data = b'[native log capture limit reached]\n'
            if self._written + len(data) > self.MAX_LOG_BYTES:return
        try:
            view = memoryview(data)
            while view:
                count = os.write(self._log_fd, view)
                if count <= 0:raise OSError('Native log short write')
                view = view[count:];self._written += count
        except OSError:
            self._disabled = True; self.error = 'log_write_failed'

    def _record(self, text):
        try:
            filtered = self._filter(text)
            if not isinstance(filtered, str):raise TypeError('Native log filter must return text')
            self._write(filtered)
        except Exception:
            self.error = 'log_filter_failed'
            self._write('[native log redaction failed; output omitted]\n')

    def _consume(self, text):
        for piece in text.splitlines(keepends=True):
            # Only LF closes a record. CR/Unicode separators may be inside a
            # credential/URL and must not make independent filter fragments.
            while piece:
                end = piece.find('\n')
                part = piece if end < 0 else piece[:end + 1]
                piece = '' if end < 0 else piece[end + 1:]
                if not self._discard:
                    if len(self._buffer) + len(part) > self.MAX_LINE_CHARS:
                        self._buffer = ''; self._discard = True
                        self._write('[native log omitted overlong output line]\n')
                    else:self._buffer += part
                if end >= 0:
                    if not self._discard:self._record(self._buffer)
                    self._buffer = ''; self._discard = False

    def _read(self):
        if os.name == 'nt':
            available = self._wintypes.DWORD()
            if not self._peek(self._read_handle, None, 0, None, self._ctypes.byref(available), None):
                error = self._ctypes.get_last_error()
                if error in (109, 232):return b''
                raise self._ctypes.WinError(error)
            if not available.value:return None
            return os.read(self._read_fd, min(4096, available.value))
        try:return os.read(self._read_fd, 4096)
        except BlockingIOError:return None

    def _run(self):
        stopped_at = None
        try:
            while True:
                if self._stop.is_set():
                    if stopped_at is None:stopped_at = time.monotonic()
                    if time.monotonic() - stopped_at > .5:break
                chunk = self._read()
                if chunk == b'':break
                if chunk is None:
                    self._stop.wait(.02) if not self._stop.is_set() else time.sleep(.01)
                    continue
                self._consume(self._decoder.decode(chunk))
            self._consume(self._decoder.decode(b'', final=True))
            if self._buffer and not self._discard:self._record(self._buffer)
        except OSError:
            self.error = 'log_pipe_failed'
        finally:
            self._buffer = ''
            os.close(self._read_fd);os.close(self._log_fd)

    def finish(self):
        self.close_writer();self._stop.set()
        self.thread.join(timeout=1.5)
        if self.thread.is_alive():
            # A supplied filter is required to be synchronous and bounded. Never
            # wait indefinitely if an embedding caller violates that contract.
            self.error = 'log_filter_stalled'


class _PosixChild:
    def __init__(self, argv, cwd, env, log_fd):
        self.process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                        stdout=log_fd, stderr=log_fd, close_fds=True,
                                        start_new_session=True)
        self.pid = self.process.pid
        self.returncode = None
        self.closed = False

    def _peek(self):
        # WNOWAIT leaves the child PID reserved, even if the gateway exits before
        # its children. Do not use Popen.poll() before terminating the group.
        try:
            result = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            # A foreign waitpid()/SIGCHLD handler already reaped the child: the
            # numeric group ID is no longer trustworthy. Never signal that ID.
            self.closed = True
            self.process.returncode = self.returncode = 0
            return False
        if result is None:
            return True
        self.returncode = (-result.si_status if result.si_code != os.CLD_EXITED else result.si_status)
        return False

    @property
    def running(self):
        if self.closed:
            return False
        if self._peek():
            return True
        if not self.closed:
            # Root exited: terminate only its still-owned group before reaping.
            self.stop(grace=0)
        return False

    def stop(self, grace=2.0):
        if self.closed:
            return
        if not self._peek() and self.closed:
            return
        try:
            if grace:
                try:
                    os.killpg(self.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # Darwin returns EPERM for a group containing only an
                    # unreaped zombie. The reserved root PID is still owned.
                    if sys.platform != 'darwin' or self._peek():raise
                deadline = time.monotonic() + grace
                while time.monotonic() < deadline and self._peek():
                    time.sleep(min(.025, max(0, deadline - time.monotonic())))
            # Always kill the group, including children ignoring SIGTERM after
            # the root exits. The unreaped root prevents group-ID recycling.
            if not self.closed:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    if sys.platform != 'darwin' or self._peek():raise
                self.returncode = self.process.wait(timeout=5)
        finally:
            self.closed = True


class _WinAPI:
    """Small stdlib ctypes boundary, replaceable by deterministic unit fixtures."""
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        self.c, self.w = c, w
        k = self.k = c.WinDLL('kernel32', use_last_error=True)
        SIZE_T = c.c_size_t

        class STARTUPINFO(c.Structure):
            _fields_ = [('cb', w.DWORD), ('lpReserved', w.LPWSTR), ('lpDesktop', w.LPWSTR),
                        ('lpTitle', w.LPWSTR), ('dwX', w.DWORD), ('dwY', w.DWORD),
                        ('dwXSize', w.DWORD), ('dwYSize', w.DWORD), ('dwXCountChars', w.DWORD),
                        ('dwYCountChars', w.DWORD), ('dwFillAttribute', w.DWORD), ('dwFlags', w.DWORD),
                        ('wShowWindow', w.WORD), ('cbReserved2', w.WORD),
                        ('lpReserved2', c.POINTER(w.BYTE)), ('hStdInput', w.HANDLE),
                        ('hStdOutput', w.HANDLE), ('hStdError', w.HANDLE)]
        class STARTUPINFOEX(c.Structure):
            _fields_ = [('StartupInfo', STARTUPINFO), ('lpAttributeList', w.LPVOID)]
        class PROCESS_INFORMATION(c.Structure):
            _fields_ = [('hProcess', w.HANDLE), ('hThread', w.HANDLE),
                        ('dwProcessId', w.DWORD), ('dwThreadId', w.DWORD)]
        class BASIC_LIMIT(c.Structure):
            _fields_ = [('PerProcessUserTimeLimit', c.c_longlong), ('PerJobUserTimeLimit', c.c_longlong),
                        ('LimitFlags', w.DWORD), ('MinimumWorkingSetSize', SIZE_T),
                        ('MaximumWorkingSetSize', SIZE_T), ('ActiveProcessLimit', w.DWORD),
                        ('Affinity', SIZE_T), ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]
        class IO_COUNTERS(c.Structure):
            _fields_ = [(name, c.c_ulonglong) for name in
                        ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                         'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]
        class EXTENDED_LIMIT(c.Structure):
            _fields_ = [('BasicLimitInformation', BASIC_LIMIT), ('IoInfo', IO_COUNTERS),
                        ('ProcessMemoryLimit', SIZE_T), ('JobMemoryLimit', SIZE_T),
                        ('PeakProcessMemoryUsed', SIZE_T), ('PeakJobMemoryUsed', SIZE_T)]
        class ACCOUNTING(c.Structure):
            _fields_ = [(name, c.c_longlong) for name in
                        ('TotalUserTime', 'TotalKernelTime', 'ThisPeriodTotalUserTime', 'ThisPeriodTotalKernelTime')]
            _fields_ += [(name, w.DWORD) for name in
                         ('TotalPageFaultCount', 'TotalProcesses', 'ActiveProcesses', 'TotalTerminatedProcesses')]
        self.Startup, self.ProcessInfo = STARTUPINFOEX, PROCESS_INFORMATION
        self.ExtendedLimit, self.Accounting = EXTENDED_LIMIT, ACCOUNTING
        specs = {
            'CreateJobObjectW': ([w.LPVOID, w.LPCWSTR], w.HANDLE),
            'SetInformationJobObject': ([w.HANDLE, c.c_int, w.LPVOID, w.DWORD], w.BOOL),
            'QueryInformationJobObject': ([w.HANDLE, c.c_int, w.LPVOID, w.DWORD, w.LPVOID], w.BOOL),
            'AssignProcessToJobObject': ([w.HANDLE, w.HANDLE], w.BOOL),
            'TerminateJobObject': ([w.HANDLE, w.UINT], w.BOOL),
            'CloseHandle': ([w.HANDLE], w.BOOL),
            'CreateProcessW': ([w.LPCWSTR, w.LPWSTR, w.LPVOID, w.LPVOID, w.BOOL, w.DWORD,
                                w.LPVOID, w.LPCWSTR, w.LPVOID, w.LPVOID], w.BOOL),
            'InitializeProcThreadAttributeList': ([w.LPVOID, w.DWORD, w.DWORD, c.POINTER(SIZE_T)], w.BOOL),
            'UpdateProcThreadAttribute': ([w.LPVOID, w.DWORD, SIZE_T, w.LPVOID, SIZE_T, w.LPVOID, w.LPVOID], w.BOOL),
            'DeleteProcThreadAttributeList': ([w.LPVOID], None),
            'ResumeThread': ([w.HANDLE], w.DWORD),
            'TerminateProcess': ([w.HANDLE, w.UINT], w.BOOL),
            'WaitForSingleObject': ([w.HANDLE, w.DWORD], w.DWORD),
            'GetExitCodeProcess': ([w.HANDLE, c.POINTER(w.DWORD)], w.BOOL),
        }
        for name, (args, result) in specs.items():
            fn = getattr(k, name); fn.argtypes = args; fn.restype = result

    def _check(self, ok):
        if not ok:
            raise self.c.WinError(self.c.get_last_error())

    def create_job(self):
        job = self.k.CreateJobObjectW(None, None)  # unnamed; not inheritable
        self._check(job)
        try:
            info = self.ExtendedLimit(); info.BasicLimitInformation.LimitFlags = 0x2000
            self._check(self.k.SetInformationJobObject(job, 9, self.c.byref(info), self.c.sizeof(info)))
            return job
        except BaseException:
            self.close_handle(job)
            raise

    def create_process(self, argv, cwd, env, log_fd, input_fd):
        import msvcrt
        c, w = self.c, self.w
        size = c.c_size_t()
        self.k.InitializeProcThreadAttributeList(None, 1, 0, c.byref(size))
        if not size.value:
            raise c.WinError(c.get_last_error())
        attributes = c.create_string_buffer(size.value)
        self._check(self.k.InitializeProcThreadAttributeList(attributes, 1, 0, c.byref(size)))
        try:
            # Only the two STDIO handles may be inherited, never the Job handle
            # or unrelated inheritable handles belonging to another thread.
            handles = (w.HANDLE * 2)(msvcrt.get_osfhandle(input_fd), msvcrt.get_osfhandle(log_fd))
            self._check(self.k.UpdateProcThreadAttribute(attributes, 0, 0x20002,
                        c.cast(handles, w.LPVOID), c.sizeof(handles), None, None))
            startup = self.Startup(); startup.StartupInfo.cb = c.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput = handles[0], handles[1]
            startup.StartupInfo.hStdError = handles[1]
            startup.lpAttributeList = c.cast(attributes, w.LPVOID)
            command = c.create_unicode_buffer(subprocess.list2cmdline(argv))
            environment = c.create_unicode_buffer('\0'.join(f'{k}={v}' for k, v in sorted(env.items(), key=lambda kv: kv[0].upper())) + '\0\0')
            process = self.ProcessInfo()
            # SUSPENDED | UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT |
            # CREATE_NO_WINDOW. Do not request BREAKAWAY_FROM_JOB.
            flags = 0x4 | 0x400 | 0x80000 | 0x08000000
            self._check(self.k.CreateProcessW(argv[0], command, None, None, True, flags,
                        c.cast(environment, w.LPVOID), str(cwd), c.byref(startup), c.byref(process)))
            return process.hProcess, process.hThread, process.dwProcessId
        finally:
            self.k.DeleteProcThreadAttributeList(attributes)

    def assign(self, job, process):
        self._check(self.k.AssignProcessToJobObject(job, process))

    def resume(self, thread):
        if self.k.ResumeThread(thread) == 0xFFFFFFFF:
            raise self.c.WinError(self.c.get_last_error())

    def active(self, job):
        info = self.Accounting()
        self._check(self.k.QueryInformationJobObject(job, 1, self.c.byref(info), self.c.sizeof(info), None))
        return info.ActiveProcesses

    def poll(self, process):
        wait = self.k.WaitForSingleObject(process, 0)
        if wait == 258:
            return None
        if wait != 0:
            raise self.c.WinError(self.c.get_last_error())
        code = self.w.DWORD(); self._check(self.k.GetExitCodeProcess(process, self.c.byref(code)))
        return code.value

    def terminate_process(self, process):
        self._check(self.k.TerminateProcess(process, 1))

    def terminate_job(self, job):
        self._check(self.k.TerminateJobObject(job, 1))

    def wait(self, process):
        if self.k.WaitForSingleObject(process, 5000) not in (0,):
            raise TimeoutError('Native process did not exit after owned Job termination')

    def close_handle(self, handle):
        if handle:
            self.k.CloseHandle(handle)


class _WindowsChild:
    def __init__(self, argv, cwd, env, log_fd, api=None):
        self.api = api or _WinAPI()
        self.job = self.process = None
        self.pid = None; self.returncode = None; self.closed = False
        thread = None; input_fd = None
        try:
            self.job = self.api.create_job()
            input_fd = os.open(os.devnull, os.O_RDONLY)
            os.set_inheritable(input_fd, True); os.set_inheritable(log_fd, True)
            self.process, thread, self.pid = self.api.create_process(argv, cwd, env, log_fd, input_fd)
            self.api.assign(self.job, self.process)
            self.api.resume(thread)
        except BaseException:
            # If assignment failed, the suspended process is not in the Job.
            # Terminate it explicitly before releasing either ownership handle.
            if self.process:
                try:self.api.terminate_process(self.process)
                except OSError:pass
            if self.job:
                self.api.close_handle(self.job); self.job = None
            if self.process:
                try:self.api.wait(self.process)
                finally:self.api.close_handle(self.process); self.process = None
            self.closed = True
            raise
        finally:
            if thread:self.api.close_handle(thread)
            if input_fd is not None:os.close(input_fd)
            os.set_inheritable(log_fd, False)

    @property
    def running(self):
        if self.closed:
            return False
        self.returncode = self.api.poll(self.process)
        if self.api.active(self.job):
            return True  # launcher may exit while its owned gateway keeps running
        self.stop()
        return False

    def stop(self, grace=0):
        if self.closed:return
        job, process = self.job, self.process
        self.job = self.process = None; self.closed = True
        try:
            # Closing is the authoritative containment barrier even if explicit
            # termination fails; no child inherited this Job handle.
            try:self.api.terminate_job(job)
            finally:self.api.close_handle(job)
        finally:
            try:
                self.api.wait(process)
                self.returncode = self.api.poll(process)
            finally:self.api.close_handle(process)


class OwnedProcess:
    """A single owned tree. start/stop/close are idempotent and async.

    start() returns the owned root PID, reusing it if already running. pid becomes
    None after stop. To change arguments, explicitly stop before starting again.
    The complete child environment must be supplied; no shell expands arguments.
    """
    def __init__(self, output_filter: Callable[[str], str] | None = None):
        if output_filter is not None and not callable(output_filter):
            raise ValueError('Native output filter must be callable')
        self._output_filter = output_filter
        self._child = None;self._pump = None
        self._lock = asyncio.Lock()
        self._stopping = False
        self._returncode = None

    @property
    def running(self):
        return bool(not self._stopping and self._child is not None and self._child.running)

    @property
    def pid(self):
        return self._child.pid if self._child is not None and not self._child.closed else None

    @property
    def returncode(self):
        return self._child.returncode if self._child is not None else self._returncode

    async def start(self, argv: list[str], cwd: Path, env: dict[str, str], log_path: Path):
        async with self._lock:
            if self.running:
                return self.pid
            if self._child is not None:
                self._returncode = self._child.returncode
                self._child = None
            if self._pump is not None:
                self._pump.finish();self._pump = None
            argv, cwd, env, log_path = _validate(argv, cwd, env, log_path)
            pump = _LogPump(log_path, self._output_filter)
            try:
                # No await between create and ownership publication. Cancellation
                # cannot leave an asynchronously spawned but untracked process.
                child = (_WindowsChild if _WINDOWS else _PosixChild)(argv, cwd, env, pump.child_fd)
                self._child = child; self._pump = pump; self._returncode = None
                _OWNED.add(self)
            except BaseException:
                pump.finish()
                raise
            finally:pump.close_writer()
            return child.pid

    async def stop(self):
        async with self._lock:
            child = self._child
            if child is None:return
            pump = self._pump
            self._stopping = True
            def finish():
                try:child.stop()
                finally:
                    if pump is not None:pump.finish()
            task = asyncio.create_task(asyncio.to_thread(finish))
            try:
                cancelled = False
                while not task.done():
                    try:await asyncio.shield(task)
                    except asyncio.CancelledError:
                        # Repeated cancellations must not release the lifecycle
                        # lock while a thread still owns live process handles.
                        cancelled = True
                task.result()
                if cancelled:raise asyncio.CancelledError
            finally:
                self._returncode = child.returncode
                self._child = None; self._pump = None; self._stopping = False
                _OWNED.discard(self)

    async def close(self):
        await self.stop()

    def _exit_cleanup(self):
        child = self._child
        if child is not None and not child.closed:
            try:child.stop(grace=0)
            except (OSError, TimeoutError):pass
        if self._pump is not None:self._pump.finish()


def _cleanup_owned():
    for process in list(_OWNED):
        process._exit_cleanup()


atexit.register(_cleanup_owned)
