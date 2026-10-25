"""Windows memory and CPU benchmark reporting contracts, without model dependencies."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from tools.decision_model import benchmark as module


class BenchmarkMemory(unittest.TestCase):
    def test_windows_uses_native_peak_working_set(self):
        with patch.object(module.sys,'platform','win32'),patch.object(module,'_windows_peak_rss_bytes',return_value=123456):
            self.assertEqual(module.peak_rss_bytes(),123456)

    def test_failed_measurement_is_unknown_not_zero(self):
        with patch.object(module.sys,'platform','win32'),patch.object(module,'_windows_peak_rss_bytes',side_effect=OSError('api')):
            self.assertIsNone(module.peak_rss_bytes())

    def test_mac_peak_already_uses_bytes(self):
        with patch.object(module.sys,'platform','darwin'),patch.object(module,'resource') as resource:
            resource.getrusage.return_value.ru_maxrss=321
            self.assertEqual(module.peak_rss_bytes(),321)

    def test_linux_peak_uses_kib(self):
        with patch.object(module.sys,'platform','linux'),patch.object(module,'resource') as resource:
            resource.getrusage.return_value.ru_maxrss=321
            self.assertEqual(module.peak_rss_bytes(),321*1024)

    def test_missing_resource_is_unknown(self):
        with patch.object(module.sys,'platform','linux'),patch.object(module,'resource',None):
            self.assertIsNone(module.peak_rss_bytes())

    def test_invalid_repeats_rejected_before_loading(self):
        for value in (0,-1,True,1.2):
            with self.assertRaisesRegex(ValueError,'invalid_repeats'):
                module.benchmark('/nonexistent',value)

    def test_runtime_metadata_records_real_version_and_explicit_disable(self):
        model=SimpleNamespace(onnxruntime_version='1.23.2',policy={
            'threads':4,'denormal_as_zero':True,'intra_op_allow_spinning':False})
        result=module.runtime_metadata(model)
        self.assertEqual(result['onnxruntime_version'],'1.23.2')
        self.assertEqual(result['runtime_policy']['threads'],4)
        self.assertIs(result['runtime_policy']['denormal_as_zero'],True)
        self.assertIs(result['runtime_policy']['intra_op_allow_spinning'],False)
        self.assertEqual(result['runtime_policy']['intra_op_spinning_config_override'],'0')

    def test_missing_spin_policy_is_unknown_build_default_not_true_or_false(self):
        result=module.runtime_metadata(SimpleNamespace(policy={}))
        self.assertIsNone(result['onnxruntime_version'])
        self.assertIsNone(result['runtime_policy']['intra_op_allow_spinning'])
        self.assertIsNone(result['runtime_policy']['intra_op_spinning_config_override'])
        self.assertIs(result['runtime_policy']['denormal_as_zero'],False)

    def test_explicit_spin_true_is_recorded_but_does_not_claim_override(self):
        result=module.runtime_metadata(SimpleNamespace(onnxruntime_version='1.23.2',policy={'intra_op_allow_spinning':True}))
        self.assertIs(result['runtime_policy']['intra_op_allow_spinning'],True)
        self.assertIsNone(result['runtime_policy']['intra_op_spinning_config_override'])


if __name__=='__main__':unittest.main()
