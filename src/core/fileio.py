"""Read-only shared-delete handles and bounded snapshots for Windows logs."""
from __future__ import annotations
import os


def open_shared_read(path):
    if os.name != 'nt':
        return open(path, 'rb')
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # Share reads, appends and deletion. No write access is granted to this handle.
    handle = create(os.path.abspath(os.fspath(path)), 0x80000000, 0x7, None, 3, 0x08000080, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    try:
        return os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise


def read_snapshot(path, max_bytes):
    """Bounded read; close the OS handle before expensive parsing/tokenization."""
    with open_shared_read(path) as file:
        size = os.fstat(file.fileno()).st_size
        offset = max(0, size - max_bytes)
        file.seek(offset)
        return file.read(max_bytes), offset
