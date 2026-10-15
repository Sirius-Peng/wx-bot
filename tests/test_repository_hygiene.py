"""Offline checks for reproducible exports and a minimal build context."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(os.environ.get("WXBOT_TEST_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))
from src.personas.phrolova import CORE_PROMPT, EXAMPLES, RELATIONSHIPS
from tools.export_persona import export, training_rows


class RepositoryHygieneTests(unittest.TestCase):
    def test_training_rows_keep_every_example_and_relationship(self):
        rows = list(training_rows())
        self.assertEqual(len(rows), len(EXAMPLES))
        for row, example in zip(rows, EXAMPLES):
            self.assertEqual(row, {"messages": [
                {"role": "system", "content": CORE_PROMPT + "\n" + RELATIONSHIPS[example["relationship"]]},
                {"role": "user", "content": example["user"]},
                {"role": "assistant", "content": example["assistant"]},
            ]})

    def test_export_retains_provenance_and_unicode(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = export(Path(temp) / "中文 语料")
            rows = [json.loads(line) for line in paths[0].read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows, EXAMPLES)
            self.assertEqual(paths[1].read_bytes().count(b"\n"), 100)
            self.assertNotIn(b"\r\n", paths[1].read_bytes())

    def test_explicit_force_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = export(Path(temp))
            before = [path.read_bytes() for path in paths]
            export(Path(temp), force=True)
            self.assertEqual([path.read_bytes() for path in paths], before)

    def test_existing_second_file_prevents_partial_export(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "train.jsonl").write_text("keep me", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                export(root)
            self.assertFalse((root / "corpus.jsonl").exists())
            self.assertEqual((root / "train.jsonl").read_text(), "keep me")

    def test_force_does_not_follow_file_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "original.txt"
            target.write_text("keep me")
            link = root / "train.jsonl"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("file symlinks unavailable")
            with self.assertRaises(FileExistsError):
                export(root, force=True)
            self.assertFalse((root / "corpus.jsonl").exists())
            self.assertEqual(target.read_text(), "keep me")

    def test_cli_works_outside_repository_without_site_packages(self):
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([
                sys.executable, "-S", str(ROOT / "tools/export_persona.py"),
                "--output-dir", str(Path(temp) / "export"),
            ], cwd=temp, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("EXPORT PASS examples=100 files=2", result.stdout)
            self.assertEqual(sorted(p.name for p in (Path(temp) / "export").iterdir()), ["corpus.jsonl", "train.jsonl"])

    def test_force_does_not_create_target_of_dangling_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "missing.txt"
            try:
                (root / "train.jsonl").symlink_to(target)
            except OSError:
                self.skipTest("file symlinks unavailable")
            with self.assertRaises(FileExistsError):
                export(root, force=True)
            self.assertFalse(target.exists())
            self.assertFalse((root / "corpus.jsonl").exists())

    def test_generated_files_and_legacy_dependencies_absent(self):
        for name in ["src/requirements.txt", "personas/phrolova/corpus.jsonl", "personas/phrolova/train.jsonl"]:
            self.assertFalse((ROOT / name).exists(), name)
        self.assertIn("personas/phrolova/generated/", (ROOT / ".gitignore").read_text())

    def test_docker_context_allowlist_matches_copy_inputs(self):
        patterns = [line for line in (ROOT / ".dockerignore").read_text().splitlines() if line and not line.startswith("#")]
        self.assertEqual(patterns, ["**", "!Dockerfile", "!requirements.txt", "!src/", "!src/**", "**/__pycache__/", "**/*.py[cod]", "**/.DS_Store", "**/Thumbs.db"])
        copies = [line for line in (ROOT / "Dockerfile").read_text().splitlines() if line.startswith("COPY ")]
        self.assertEqual(copies, ["COPY requirements.txt .", "COPY src/ ./src/"])

    def test_desktop_icons_use_one_external_resource_copy(self):
        config = (ROOT / "desktop/electron-builder.yml").read_text()
        self.assertNotIn("  - assets/**/*", config)
        self.assertEqual(config.count("  - from: assets\n    to: assets"), 1)
        paths = (ROOT / "desktop/src/main/paths.js").read_text()
        self.assertIn("if (isPackaged()) return path.join(process.resourcesPath, 'assets');", paths)
        for filename in ["icon.png", "icon.icns", "icon.ico", "tray.png", "trayTemplate.png", "trayTemplate@2x.png"]:
            self.assertTrue((ROOT / "desktop/assets" / filename).is_file())


if __name__ == "__main__":
    unittest.main()
