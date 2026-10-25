"""Offline contracts only; Windows/CUDA execution must be reported separately."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools/decision_model/train-windows.ps1'


class WindowsTrainingRunnerTests(unittest.TestCase):
    def setUp(self):
        self.source = SCRIPT.read_text(encoding='utf-8-sig')

    def test_training_and_runtime_environments_are_distinct(self):
        self.assertIn('[string]$TrainPython', self.source)
        self.assertIn('[string]$RuntimePython', self.source)
        self.assertIn('$TrainPython.Equals($RuntimePython', self.source)
        self.assertIn("find_spec('torch') is None", self.source)
        self.assertIn("'CPUExecutionProvider' in ort.get_available_providers()", self.source)

    def test_inline_native_environment_probes_are_valid_python(self):
        blocks = re.findall(r"\$(?:trainProbe|runtimeProbe) = @'\n(.*?)\n'@", self.source, re.S)
        self.assertEqual(len(blocks), 2)
        for block in blocks:
            compile(block, str(SCRIPT), 'exec')
            self.assertIn("os.name=='nt'", block)
            self.assertIn("struct.calcsize('P')==8", block)
        self.assertIn("x=torch.ones((2,2),device='cuda')", blocks[0])

    def test_no_implicit_cpu_fallback_or_platform_changes(self):
        self.assertIn("[string]$Device = 'cuda'", self.source)
        self.assertIn("if device=='cuda': assert available", self.source)
        for command in ('Restart-Computer', 'Stop-Computer', 'winget ', 'pip install', 'nvidia-smi -r'):
            self.assertNotIn(command, self.source)

    def test_new_output_even_for_resume_and_preserved_inputs(self):
        for name in ('WorkspaceRoot', 'CacheRoot', 'DataRoot', 'OutputRoot', 'BaseDir', 'Resume', 'RunName'):
            self.assertIn('[string]$' + name, self.source)
        self.assertIn("throw 'Run already exists; choose a new RunName, also when resuming.'", self.source)
        self.assertIn("throw 'PrepareData requires a new DataRoot; existing corpora are immutable inputs.'", self.source)
        self.assertIn("'--resume', $Resume", self.source)
        self.assertIn("Assert-External $DataRoot 'DataRoot'", self.source)
        self.assertIn("input_sha256['resume/training.json']", self.source)
        self.assertNotIn('Remove-Item', self.source)

    def test_cpu_benchmark_is_saved_before_failed_quality_evaluation(self):
        steps = re.findall(r"Invoke-Stage '(0[3-9]-[^']+)' (\$\w+)", self.source)
        self.assertEqual(steps, [
            ('03-fetch', '$TrainPython'), ('04-train', '$TrainPython'),
            ('05-calibrate', '$TrainPython'), ('06-export', '$TrainPython'),
            ('07-benchmark-cpu', '$RuntimePython'), ('08-evaluate', '$TrainPython'),
            ('09-package-check', '$RuntimePython'),
        ])
        self.assertIn("$_.Exception.Data['Stage'] -eq '08-evaluate' -and $failureCode -eq 2", self.source)
        self.assertIn("'quality_failed'", self.source)
        self.assertIn('exit $script:ExitCode', self.source)

    def test_runs_keep_status_logs_and_no_release_copy(self):
        for field in ('run-state.json', 'training-environment.json', 'runtime-environment.json',
                      'benchmark-windows-cpu.json', 'source_sha256', 'input_sha256', 'exit_code'):
            self.assertIn(field, self.source)
        self.assertIn('Tee-Object -FilePath', self.source)
        self.assertIn('$failure.Data[\'ExitCode\']', self.source)
        self.assertIn('release_accepted = $false', self.source)
        self.assertNotIn('Copy-Item', self.source)


if __name__ == '__main__':
    unittest.main()
