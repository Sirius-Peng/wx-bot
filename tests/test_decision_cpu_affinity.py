"""No-model Windows affinity contracts with mock APIs and actual CPU-set layout."""
import copy
import ctypes
import struct
import types
import unittest
from unittest.mock import patch

from src.core import local_decision_cpu as cpu


def topology():
    return [dict(id=256+i, group=0, logical_processor_index=i,
                 core_index=i//2 if i<8 else i, efficiency_class=2 if i<8 else 0,
                 parked=bool(i%2) if i<8 else False,
                 allocated=False, allocated_to_target_process=False) for i in range(12)]


class FakeWindowsSelf:
    def __init__(self, records=None, original=0xfff, system=0xfff):
        self.records = topology() if records is None else records
        self.mask = original
        self.system = system
        self.set_calls = []
        self.affinity_calls = 0
        self.groups = 1
        self.affinity_overrides = {}
        self.set_failures = set()
        self.partial_failures = set()
        self.discovery_error = None

    def group_count(self):
        return self.groups

    def affinity(self):
        self.affinity_calls += 1
        result = self.affinity_overrides.get(self.affinity_calls)
        if isinstance(result, Exception):
            raise result
        return result if result is not None else (self.mask, self.system)

    def cpu_sets(self):
        if self.discovery_error:
            raise self.discovery_error
        return copy.deepcopy(self.records)

    def set_affinity(self, mask):
        self.set_calls.append(mask)
        number = len(self.set_calls)
        if number in self.set_failures:
            raise OSError('native set failed without changing affinity')
        self.mask = mask
        if number in self.partial_failures:
            raise OSError('wrapper failed after native set')


def raw_record(identifier=256, group=0, logical=3, core=2, efficiency=9, flags=7, size=32):
    # Official variable-size header + CPU_SET fields at offsets 8..31.
    result = (struct.pack('<II', size, 0)
              + struct.pack('<IHBBBBBB', identifier, group, logical, core, 5, 6, efficiency, flags)
              + struct.pack('<IQ', 0x12345678, 0x1020304050607080))
    return result + b'\x91' * max(0, size-len(result))


class CpuAffinitySelectionTests(unittest.TestCase):
    def test_all_highest_class_cpus_include_parked_smt(self):
        selection, reason = cpu._select(topology(), 0xfff, 0xfff)
        self.assertIsNone(reason)
        self.assertEqual(selection['mask'], 0xff)
        self.assertEqual(selection['logical_processors'], list(range(8)))
        self.assertEqual(selection['parked_logical_processors'], [1,3,5,7])
        self.assertEqual(len(selection['physical_cores']), 4)

    def test_even_all_parked_performance_cores_are_selected(self):
        rows = topology()
        for row in rows:
            if row['efficiency_class']==2:
                row['parked'] = True
        selection, reason = cpu._select(rows, 0xfff, 0xfff)
        self.assertIsNone(reason)
        self.assertEqual(selection['mask'], 0xff)
        self.assertEqual(selection['parked_logical_processors'], list(range(8)))

    def test_hybrid_is_detected_from_system_not_already_p_only_original(self):
        selection, reason = cpu._select(topology(), 0xaa, 0xfff)
        self.assertIsNone(reason)
        self.assertEqual(selection['mask'], 0xaa)
        self.assertEqual(len(selection['physical_cores']), 4)

    def test_smt_logicals_do_not_inflate_physical_minimum(self):
        self.assertEqual(cpu._select(topology(), 0x3f, 0xfff),
                         (None, 'insufficient_performance_physical_cores'))

    def test_original_mask_is_never_widened(self):
        selection, reason = cpu._select(topology(), 0x555, 0xfff)
        self.assertIsNone(reason)
        self.assertEqual(selection['mask'], 0x55)
        self.assertEqual(selection['mask'] & ~0x555, 0)

    def test_reserved_other_process_cpus_are_excluded_not_target_owned(self):
        rows = topology()
        rows[1].update(allocated=True, allocated_to_target_process=False)
        rows[3].update(allocated=True, allocated_to_target_process=True)
        selection, reason = cpu._select(rows, 0xfff, 0xfff)
        self.assertIsNone(reason)
        self.assertFalse(selection['mask'] & 2)
        self.assertTrue(selection['mask'] & 8)

    def test_no_lower_class_substitution_when_allowed_p_cores_insufficient(self):
        self.assertEqual(cpu._select(topology(), 0xf00, 0xfff),
                         (None, 'insufficient_performance_physical_cores'))

    def test_homogeneous_cpu_is_untouched(self):
        rows = topology()
        for row in rows:
            row['efficiency_class'] = 1
        self.assertEqual(cpu._select(rows, 0xfff, 0xfff), (None, 'not_hybrid'))

    def test_outside_system_mask_cannot_create_fake_highest_class(self):
        rows = topology()+[dict(topology()[0], id=900, logical_processor_index=12, efficiency_class=255)]
        selection, reason = cpu._select(rows, 0xfff, 0xfff)
        self.assertIsNone(reason)
        self.assertEqual(selection['mask'], 0xff)

    def test_bad_masks_and_topology_abstain(self):
        for original, system in [(0,0xfff),(-1,0xfff),(0x1000,0xfff),(True,0xfff),
                                 (0xfff,1<<(struct.calcsize('P')*8))]:
            self.assertEqual(cpu._select(topology(), original, system), (None,'invalid_original_affinity'))
        rows = topology()
        rows[0]['logical_processor_index'] = rows[1]['logical_processor_index']
        self.assertEqual(cpu._select(rows,0xfff,0xfff),(None,'invalid_cpu_topology'))
        rows = topology()
        rows[0]['logical_processor_index'] = struct.calcsize('P')*8
        self.assertEqual(cpu._select(rows,0xfff,0xfff),(None,'invalid_cpu_topology'))
        rows = topology()
        rows[0]['group'] = 1
        self.assertEqual(cpu._select(rows,0xfff,0xfff),(None,'unsupported_cpu_set_groups'))


class CpuAffinityRuntimeTests(unittest.TestCase):
    def apply(self, api):
        with patch.object(cpu.sys, 'platform', 'win32'), patch.object(cpu, '_WindowsSelf', return_value=api):
            return cpu.prefer_performance_cores(True)

    def test_disabled_and_non_windows_never_call_native_api(self):
        with patch.object(cpu, '_WindowsSelf') as native:
            self.assertEqual(cpu.prefer_performance_cores(False)['reason'], 'disabled')
            with patch.object(cpu.sys,'platform','darwin'):
                self.assertEqual(cpu.prefer_performance_cores(True)['reason'],'not_windows')
            native.assert_not_called()
        for value in (1,0,'true',None):
            with self.assertRaisesRegex(ValueError,'invalid_runtime_policy'):
                cpu.prefer_performance_cores(value)

    def test_applies_only_discovered_subset_and_reports_parked(self):
        api = FakeWindowsSelf()
        result = self.apply(api)
        self.assertTrue(result['applied'])
        self.assertEqual(api.set_calls,[0xff])
        self.assertEqual(result['original_mask'],'0xfff')
        self.assertEqual(result['actual_mask'],'0xff')
        self.assertEqual(result['system_mask'],'0xfff')
        self.assertEqual(result['parked_logical_processors'],[1,3,5,7])
        self.assertEqual(result['policy'],'highest_efficiency_class_all_allowed_original_mask')

    def test_already_p_only_mask_is_recognized_without_native_write(self):
        for original in (0xff,0x55,0xaa):
            with self.subTest(original=original):
                api = FakeWindowsSelf(original=original)
                result = self.apply(api)
                self.assertEqual(result['reason'],'already_preferred')
                self.assertEqual(result['actual_mask'],hex(original))
                self.assertFalse(result['applied'])
                self.assertEqual(api.set_calls,[])

    def test_multi_group_and_missing_topology_no_change(self):
        for records, groups in [(topology(),2),([],1)]:
            api = FakeWindowsSelf(records=records)
            api.groups = groups
            result = self.apply(api)
            self.assertFalse(result['applied'])
            self.assertEqual(api.mask,0xfff)
            self.assertEqual(api.set_calls,[])

    def test_discovery_api_failure_leaves_original_mask(self):
        api = FakeWindowsSelf()
        api.discovery_error = OSError('topology query denied')
        result = self.apply(api)
        self.assertEqual(result['reason'],'windows_api_failed')
        self.assertEqual(api.mask,0xfff)
        self.assertEqual(api.set_calls,[])

    def test_concurrent_external_change_is_not_overwritten(self):
        api = FakeWindowsSelf()
        api.affinity_overrides[2] = (0x55,0xfff)
        result = self.apply(api)
        self.assertEqual(result['reason'],'affinity_changed_during_discovery')
        self.assertEqual(result['actual_mask'],'0x55')
        self.assertEqual(api.set_calls,[])

    def test_failed_set_without_effect_does_not_write_a_second_mask(self):
        api = FakeWindowsSelf()
        api.set_failures.add(1)
        result = self.apply(api)
        self.assertEqual(result['reason'],'windows_api_failed')
        self.assertTrue(result['original_affinity_preserved'])
        self.assertEqual(api.mask,0xfff)
        self.assertEqual(api.set_calls,[0xff])

    def test_wrapper_error_after_native_change_restores_exact_original(self):
        api = FakeWindowsSelf(original=0x555)
        api.partial_failures.add(1)
        result = self.apply(api)
        self.assertEqual(result['reason'],'verification_failed_restored')
        self.assertTrue(result['restored'])
        self.assertEqual(api.mask,0x555)
        self.assertEqual(api.set_calls,[0x55,0x555])

    def test_readback_mismatch_or_error_restores_original(self):
        for problem in [(0x3f,0xfff),OSError('readback failed')]:
            api = FakeWindowsSelf()
            api.affinity_overrides[3] = problem
            result = self.apply(api)
            self.assertEqual(result['reason'],'verification_failed_restored')
            self.assertTrue(result['restored'])
            self.assertEqual(api.mask,0xfff)
            self.assertEqual(api.set_calls,[0xff,0xfff])

    def test_restore_error_is_reported_not_claimed_success(self):
        api = FakeWindowsSelf()
        api.affinity_overrides[3] = OSError('readback failed')
        api.set_failures.add(2)
        result = self.apply(api)
        self.assertFalse(result['restored'])
        self.assertFalse(result['applied'])
        self.assertEqual(result['reason'],'verification_failed_restore_failed')
        self.assertIsNone(result['actual_mask'])


class CpuSetLayoutTests(unittest.TestCase):
    def test_real_offsets_flags_and_variable_size_records(self):
        unknown = struct.pack('<II',8,44)
        data = unknown+raw_record()+raw_record(identifier=257,logical=7,core=6,efficiency=1,flags=0,size=48)
        parsed = cpu.parse_cpu_sets(data)
        self.assertEqual(parsed,[dict(id=256,group=0,logical_processor_index=3,core_index=2,
                                     efficiency_class=9,parked=True,allocated=True,allocated_to_target_process=True),
                                 dict(id=257,group=0,logical_processor_index=7,core_index=6,
                                     efficiency_class=1,parked=False,allocated=False,allocated_to_target_process=False)])

    def test_raw_flags_are_independent(self):
        for flags in range(8):
            row = cpu.parse_cpu_sets(raw_record(flags=flags))[0]
            self.assertEqual(row['parked'],bool(flags&1))
            self.assertEqual(row['allocated'],bool(flags&2))
            self.assertEqual(row['allocated_to_target_process'],bool(flags&4))

    def test_empty_and_unknown_records_are_valid(self):
        self.assertEqual(cpu.parse_cpu_sets(b''),[])
        self.assertEqual(cpu.parse_cpu_sets(struct.pack('<II',8,17)),[])

    def test_truncation_invalid_size_and_short_known_record_rejected(self):
        for raw in (b'\0'*7,struct.pack('<II',0,0),struct.pack('<II',7,0),
                    struct.pack('<II',64,0)+b'\0'*24,struct.pack('<II',31,0)+b'\0'*23,
                    raw_record()+b'\0'*3):
            with self.subTest(length=len(raw)):
                with self.assertRaises(ValueError):
                    cpu.parse_cpu_sets(raw)

    def test_native_buffer_retry_uses_raw_layout_without_windows(self):
        raw = raw_record()
        calls = []
        state = {'error':122}
        def get_sets(buffer, capacity, required, process, flags):
            calls.append(capacity)
            required._obj.value = len(raw)
            if buffer is None:
                return 0
            ctypes.memmove(buffer,raw,len(raw))
            return 1
        api = cpu._WindowsSelf.__new__(cpu._WindowsSelf)
        api.ctypes = types.SimpleNamespace(c_uint32=ctypes.c_uint32,byref=ctypes.byref,
            create_string_buffer=ctypes.create_string_buffer,get_last_error=lambda:state['error'],
            WinError=lambda code:OSError(code,'mock native failure'))
        api.kernel = types.SimpleNamespace(GetSystemCpuSetInformation=get_sets)
        api.handle = -1
        self.assertEqual(api.cpu_sets(),cpu.parse_cpu_sets(raw))
        self.assertEqual(calls,[0,32])


if __name__=='__main__':
    unittest.main()
