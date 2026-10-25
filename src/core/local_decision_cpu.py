"""Opt-in affinity for THIS decision-worker process, never another PID.

Windows CPU-set topology is queried at runtime. Only a single processor group is
supported; the original process mask is an upper bound. No priority, parent
process, system setting or hardcoded processor number is changed. Native calls
are isolated from the pure selection logic so contracts can be tested with mocks.
"""
from __future__ import annotations

import os
import struct
import sys


def parse_cpu_sets(raw):
    """Parse variable-size SYSTEM_CPU_SET_INFORMATION, skipping unknown types."""
    records = []
    offset = 0
    while offset < len(raw):
        if len(raw)-offset < 8:
            raise ValueError('truncated_cpu_set_header')
        size, kind = struct.unpack_from('<II', raw, offset)
        if size < 8 or offset+size > len(raw):
            raise ValueError('invalid_cpu_set_size')
        if kind == 0:  # CpuSetInformation
            if size < 32:
                raise ValueError('truncated_cpu_set_record')
            identifier, group, logical, core, _, _, efficiency, flags = struct.unpack_from('<IHBBBBBB', raw, offset+8)
            records.append({'id': identifier, 'group': group, 'logical_processor_index': logical,
                            'core_index': core, 'efficiency_class': efficiency,
                            'parked': bool(flags & 1), 'allocated': bool(flags & 2),
                            'allocated_to_target_process': bool(flags & 4)})
        offset += size
    return records


class _WindowsSelf:
    """Pseudo-handle APIs address only GetCurrentProcess; no PID input exists."""
    def __init__(self):
        import ctypes
        self.ctypes = ctypes
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        k = self.kernel
        k.GetCurrentProcess.argtypes = []
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.GetActiveProcessorGroupCount.argtypes = []
        k.GetActiveProcessorGroupCount.restype = ctypes.c_uint16
        k.GetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
        k.GetProcessAffinityMask.restype = ctypes.c_int32
        k.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        k.SetProcessAffinityMask.restype = ctypes.c_int32
        k.GetSystemCpuSetInformation.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p, ctypes.c_uint32]
        k.GetSystemCpuSetInformation.restype = ctypes.c_int32
        # GetCurrentProcess returns a pseudo-handle: do not CloseHandle it.
        self.handle = k.GetCurrentProcess()

    def group_count(self):
        count = int(self.kernel.GetActiveProcessorGroupCount())
        if count == 0:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return count

    def affinity(self):
        c = self.ctypes
        process, system = c.c_size_t(), c.c_size_t()
        if not self.kernel.GetProcessAffinityMask(self.handle, c.byref(process), c.byref(system)):
            raise c.WinError(c.get_last_error())
        return int(process.value), int(system.value)

    def cpu_sets(self):
        c = self.ctypes
        required = c.c_uint32()
        ok = self.kernel.GetSystemCpuSetInformation(None, 0, c.byref(required), self.handle, 0)
        if not ok and c.get_last_error() != 122:  # ERROR_INSUFFICIENT_BUFFER
            raise c.WinError(c.get_last_error())
        if ok and required.value == 0:
            return []
        for _ in range(3):
            if not 0 < required.value <= 16*1024*1024:
                raise ValueError('invalid_cpu_set_buffer_size')
            capacity = int(required.value)
            buffer = c.create_string_buffer(capacity)
            if self.kernel.GetSystemCpuSetInformation(buffer, capacity, c.byref(required), self.handle, 0):
                if required.value > capacity:
                    raise ValueError('cpu_set_buffer_overflow')
                return parse_cpu_sets(buffer.raw[:required.value])
            if c.get_last_error() != 122:
                raise c.WinError(c.get_last_error())
        raise OSError('cpu_topology_changed_repeatedly')

    def set_affinity(self, mask):
        if not self.kernel.SetProcessAffinityMask(self.handle, mask):
            raise self.ctypes.WinError(self.ctypes.get_last_error())


def _select(records, original, system):
    if (isinstance(original, bool) or isinstance(system, bool)
            or not isinstance(original, int) or not isinstance(system, int)
            or original <= 0 or system <= 0 or original & ~system
            or system >= 1 << (struct.calcsize('P')*8)):
        return None, 'invalid_original_affinity'
    if not records or any(r['group'] != 0 for r in records):
        return None, 'unsupported_cpu_set_groups'
    logicals = [r['logical_processor_index'] for r in records]
    if (any(isinstance(n, bool) or not isinstance(n, int)
            or not 0 <= n < struct.calcsize('P')*8 for n in logicals)
            or len(set(logicals)) != len(logicals)):
        return None, 'invalid_cpu_topology'
    if any(isinstance(r['efficiency_class'], bool) or not isinstance(r['efficiency_class'], int)
           or not 0 <= r['efficiency_class'] <= 255
           or isinstance(r['core_index'], bool) or not isinstance(r['core_index'], int)
           or not 0 <= r['core_index'] <= 255 for r in records):
        return None, 'invalid_cpu_topology'
    # Querying with GetCurrentProcess only supplies allocation ownership flags;
    # Windows still returns system topology. Determine heterogeneity BEFORE
    # intersecting with our process mask, which may already contain only P cores.
    available = [r for r in records if system & (1 << r['logical_processor_index'])]
    classes = {r['efficiency_class'] for r in available}
    if len(classes) < 2:
        return None, 'not_hybrid'
    highest = max(classes)
    # Parked is a transient power state, not permission or CPU capability. Keep
    # all allowed highest-class CPUs, as measured in the all-P Windows experiment;
    # do not change parking policy or attempt to unpark a processor ourselves.
    selected = [r for r in available if r['efficiency_class'] == highest
                and original & (1 << r['logical_processor_index'])
                and not (r['allocated'] and not r['allocated_to_target_process'])]
    cores = {}
    for row in selected:
        cores.setdefault(row['core_index'], []).append(row['logical_processor_index'])
    if len(cores) < 4:
        return None, 'insufficient_performance_physical_cores'
    mask = sum(1 << r['logical_processor_index'] for r in selected)
    return {'mask': mask, 'efficiency_class': highest,
            'physical_cores': [{'group': 0, 'core_index': core, 'logical_processors': sorted(logicals)}
                               for core, logicals in sorted(cores.items())],
            'logical_processors': sorted(r['logical_processor_index'] for r in selected),
            'parked_logical_processors': sorted(r['logical_processor_index'] for r in selected if r['parked'])}, None


def prefer_performance_cores(enabled=False):
    """Optionally narrow only our process; false/non-Windows leaves it untouched."""
    if not isinstance(enabled, bool):
        raise ValueError('invalid_runtime_policy:prefer_performance_cores')
    report = {'enabled': enabled, 'pid': os.getpid(), 'platform': sys.platform,
              'policy': 'highest_efficiency_class_all_allowed_original_mask',
              'applied': False, 'reason': 'disabled', 'original_mask': None,
              'selected_mask': None, 'actual_mask': None, 'physical_cores': [],
              'logical_processors': [], 'parked_logical_processors': [], 'efficiency_class': None}
    if not enabled:
        return report
    if sys.platform != 'win32':
        report['reason'] = 'not_windows'
        return report
    api = None
    attempted = False
    original = None
    try:
        api = _WindowsSelf()
        if api.group_count() != 1:
            report['reason'] = 'unsupported_processor_groups'
            return report
        original, system = api.affinity()
        report.update(original_mask=hex(original), actual_mask=hex(original), system_mask=hex(system))
        selection, reason = _select(api.cpu_sets(), original, system)
        if selection is None:
            report['reason'] = reason
            return report
        selected = selection['mask']
        report.update(selected_mask=hex(selected), physical_cores=selection['physical_cores'],
                      logical_processors=selection['logical_processors'],
                      parked_logical_processors=selection['parked_logical_processors'],
                      efficiency_class=selection['efficiency_class'])
        current, _ = api.affinity()
        if current != original:
            report.update(reason='affinity_changed_during_discovery', actual_mask=hex(current))
            return report
        if selected == original:
            report['reason'] = 'already_preferred'
            return report
        attempted = True
        api.set_affinity(selected)
        actual, _ = api.affinity()
        if actual != selected:
            raise OSError('affinity_readback_mismatch')
        report.update(applied=True, actual_mask=hex(actual), reason='applied')
    except (OSError, AttributeError, ValueError, TypeError, KeyError) as error:
        report.update(reason='windows_api_failed', error_type=type(error).__name__)
        if attempted and api is not None:
            try:
                # A failed native call normally leaves the mask unchanged. Read
                # it back before restoring, including the defensive case where a
                # wrapper raises after the native call already changed it.
                current, _ = api.affinity()
                if current == original:
                    report.update(actual_mask=hex(original), original_affinity_preserved=True)
                    return report
            except (OSError, AttributeError, ValueError, TypeError):
                pass
            try:
                api.set_affinity(original)
                actual, _ = api.affinity()
                report.update(actual_mask=hex(actual), restored=actual == original,
                              reason='verification_failed_restored' if actual == original else 'verification_failed_restore_mismatch')
            except (OSError, AttributeError, ValueError, TypeError):
                report.update(actual_mask=None, restored=False, reason='verification_failed_restore_failed')
    return report
