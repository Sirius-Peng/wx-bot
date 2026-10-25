"""Lightweight training CLI/provenance contracts; never imports Torch or trains."""
import copy
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.decision_model import pipeline


class TrainingWindowsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def fake_torch(self, cuda=True, mps=True):
        return types.SimpleNamespace(
            cuda=types.SimpleNamespace(is_available=lambda: cuda),
            backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: mps)),
        )

    def args(self, **changes):
        values = dict(out=str(self.root/'new-run'), data=str(self.root/'data'),
                      epochs=3, batch=16, lr=2e-5, limit=None, resume=None)
        values.update(changes)
        return types.SimpleNamespace(**values)

    def checkpoint(self, **changes):
        path = self.root/'task-head.safetensors'
        path.write_bytes(b'unit-test weight fixture')
        metadata = dict(base=pipeline.BASE_REPO, revision=pipeline.BASE_REV,
                        base_sha256=pipeline.BASE_SHA, sources=copy.deepcopy(pipeline.SOURCES),
                        epochs=[{'epoch': 1}], training='legacy stale prose')
        metadata.update(changes)
        (self.root/'training.json').write_text(json.dumps(metadata), encoding='utf-8')
        return path

    def test_auto_prefers_cuda_over_mps(self):
        self.assertEqual(pipeline.select_device(self.fake_torch()), 'cuda')
        self.assertEqual(pipeline.select_device(self.fake_torch(cuda=False)), 'mps')
        self.assertEqual(pipeline.select_device(self.fake_torch(False, False)), 'cpu')
        self.assertEqual(pipeline.select_device(self.fake_torch(), 'cpu'), 'cpu')

    def test_explicit_gpu_does_not_silently_fall_back(self):
        with self.assertRaisesRegex(ValueError, 'cuda_not_available'):
            pipeline.select_device(self.fake_torch(False), 'cuda')
        with self.assertRaisesRegex(ValueError, 'mps_not_available'):
            pipeline.select_device(self.fake_torch(False, False), 'mps')
        with self.assertRaisesRegex(ValueError, 'invalid_device'):
            pipeline.select_device(self.fake_torch(), 'gpu')

    def test_legacy_resume_has_hashed_provenance_not_old_prose_architecture(self):
        path = self.checkpoint()
        result = pipeline.resume_metadata(path)
        self.assertTrue(result['legacy_architecture_metadata'])
        self.assertEqual(result['mode'], 'weights_only_new_optimizer')
        self.assertEqual(result['source_epochs'], 1)
        self.assertEqual(result['sha256'], pipeline.sha(path))
        self.assertEqual(result['source_training_description'], 'legacy stale prose')

    def test_current_resume_architecture_accepted(self):
        result = pipeline.resume_metadata(self.checkpoint(architecture=pipeline.POLICY['architecture']))
        self.assertFalse(result['legacy_architecture_metadata'])

    def test_resume_rejects_different_base_revision_hash_source_and_head(self):
        for field, bad in [('base', 'other'), ('revision', 'other'),
                           ('base_sha256', 'other'), ('sources', {}),
                           ('architecture', 'candidate-choice-v2')]:
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, 'resume_.*_mismatch'):
                    pipeline.resume_metadata(self.checkpoint(**{field: bad}))

    def test_new_checkpoint_hash_binding_rejects_mutation(self):
        path = self.checkpoint()
        self.checkpoint(checkpoint_sha256=pipeline.sha(path))
        self.assertTrue(pipeline.resume_metadata(path)['checkpoint_metadata_hash_bound'])
        path.write_bytes(b'changed weight fixture')
        with self.assertRaisesRegex(ValueError, 'resume_checkpoint_hash_mismatch'):
            pipeline.resume_metadata(path)

    def test_resume_requires_adjacent_provenance(self):
        path = self.root/'missing.safetensors'
        with self.assertRaisesRegex(ValueError, 'resume_checkpoint_missing'):
            pipeline.resume_metadata(path)
        path.write_bytes(b'dummy')
        with self.assertRaisesRegex(ValueError, 'resume_training_metadata_missing'):
            pipeline.resume_metadata(path)

    def test_resume_tensor_keys_and_shapes_are_exact(self):
        tensor = lambda shape: types.SimpleNamespace(shape=shape)
        expected = {'classifier.0.weight': tensor((768,)),
                    'classifier.4.weight': tensor((3, 384)),
                    'encoder.layers.18.weight': tensor((768, 768)),
                    'encoder.layers.0.weight': tensor((768, 768))}
        good = {k: v for k, v in expected.items() if pipeline.trainable_key(k)}
        pipeline.validate_resume_tensors(good, expected)
        for wrong in [{}, {**good, 'head.weight': tensor((3, 768))},
                      {**good, 'encoder.layers.0.weight': tensor((768, 768))}]:
            with self.assertRaisesRegex(ValueError, 'resume_tensor_keys_mismatch'):
                pipeline.validate_resume_tensors(wrong, expected)
        with self.assertRaisesRegex(ValueError, 'resume_tensor_shape_mismatch'):
            pipeline.validate_resume_tensors({**good, 'classifier.4.weight': tensor((2, 384))}, expected)

    def test_only_four_encoder_layers_and_typed_head_train(self):
        for name in ('classifier.0.weight', 'encoder.layers.18.norm.weight',
                     'encoder.layers.19.weight', 'encoder.layers.20.weight', 'encoder.layers.21.weight'):
            self.assertTrue(pipeline.trainable_key(name), name)
        for name in ('encoder.layers.17.weight', 'encoder.layers.22.weight',
                     'encoder.layers.180.weight', 'act_head.0.weight', 'head.weight'):
            self.assertFalse(pipeline.trainable_key(name), name)

    def test_existing_runs_never_overwrite(self):
        args = self.args()
        Path(args.out).mkdir()
        pipeline.validate_train_args(args)
        for name in ('training.json', 'task-head.safetensors', 'checkpoints'):
            path = Path(args.out)/name
            path.write_text('preserve')
            with self.assertRaisesRegex(ValueError, 'training_output_exists_choose_new_run'):
                pipeline.validate_train_args(args)
            self.assertEqual(path.read_text(), 'preserve')
            path.unlink()

    def test_invalid_training_args_fail_before_torch_import(self):
        for changes in ({'epochs': 0}, {'batch': 0}, {'lr': 0}, {'lr': float('nan')}, {'lr': float('inf')}, {'limit': 0}, {'data': None}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    pipeline.validate_train_args(self.args(**changes))

    def test_local_base_verifies_hash_without_huggingface_import(self):
        base = self.root/'base'
        base.mkdir()
        (base/'model.safetensors').write_bytes(b'base fixture')
        with patch.object(pipeline, 'BASE_SHA', pipeline.sha(base/'model.safetensors')):
            self.assertEqual(pipeline.fetch('unused', base), base.resolve())
        with self.assertRaisesRegex(ValueError, 'base_hash'):
            pipeline.fetch('unused', base)

    def test_export_provenance_uses_recorded_training_not_new_dataset_claims(self):
        root=self.root/'export'
        root.mkdir()
        (root/'task-head.safetensors').write_bytes(b'fixed task checkpoint')
        metadata={'split_sha256':{'train':'a'*64,'calibration':'b'*64},'resume':None,
                  'dataset':{'splits':{'train':{'sha256':'c'*64}}}}
        (root/'training.json').write_text(json.dumps(metadata))
        result=pipeline.export_training_provenance(root)
        self.assertTrue(result['history_complete'])
        self.assertEqual(result['split_sha256']['train'],'a'*64)
        self.assertEqual(result['checkpoint_sha256'],pipeline.sha(root/'task-head.safetensors'))
        self.assertEqual(result['training_metadata_sha256'],pipeline.sha(root/'training.json'))
        metadata['resume']={'sha256':'d'*64}
        (root/'training.json').write_text(json.dumps(metadata))
        self.assertFalse(pipeline.export_training_provenance(root)['history_complete'])
        metadata.pop('split_sha256')
        metadata['resume']=None
        (root/'training.json').write_text(json.dumps(metadata))
        result=pipeline.export_training_provenance(root)
        self.assertFalse(result['history_complete'])
        self.assertEqual(result['split_sha256'],{})

    def test_help_exposes_gpu_resume_local_base_without_training_imports(self):
        proc = subprocess.run([sys.executable, '-m', 'tools.decision_model.pipeline', '--help'],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for flag in ('--device', '--resume', '--base-dir', '--threads', '--no-deterministic'):
            self.assertIn(flag, proc.stdout)

    def test_configure_records_cuda_and_reproducibility(self):
        calls = {}
        torch = self.fake_torch()
        torch.__version__ = '2.8.0+cu128'
        torch.version = types.SimpleNamespace(cuda='12.8')
        torch.manual_seed = lambda x: calls.update(seed=x)
        torch.set_num_threads = lambda x: calls.update(threads=x)
        torch.use_deterministic_algorithms = lambda x: calls.update(deterministic=x)
        torch.cuda.manual_seed_all = lambda x: calls.update(cuda_seed=x)
        torch.cuda.current_device = lambda: 0
        torch.cuda.get_device_properties = lambda _: types.SimpleNamespace(name='Fixture GPU', total_memory=12*1024**3)
        torch.cuda.get_device_capability = lambda: (12, 0)
        torch.backends.cuda = types.SimpleNamespace(
            enable_flash_sdp=lambda x: calls.update(flash=x),
            enable_mem_efficient_sdp=lambda x: calls.update(efficient=x))
        torch.backends.cudnn = types.SimpleNamespace()
        args = types.SimpleNamespace(seed=215, device='cuda', threads=0, deterministic=True)
        with patch.object(pipeline.os, 'cpu_count', return_value=24):
            env = pipeline.configure_training(torch, args)
        self.assertEqual(env['threads'], 4)
        self.assertEqual(env['gpu']['name'], 'Fixture GPU')
        self.assertEqual(calls, dict(seed=215, threads=4, cuda_seed=215,
                                    deterministic=True, flash=False, efficient=False))
        self.assertFalse(torch.backends.cudnn.benchmark)
        self.assertTrue(torch.backends.cudnn.deterministic)


if __name__ == '__main__':
    unittest.main()
