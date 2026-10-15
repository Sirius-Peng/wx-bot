"""Export reproducible synthetic persona data without storing runtime duplicates.

Run from any directory: python tools/export_persona.py [--output-dir DIR] [--force]
This command uses only the standard library and never calls a model.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.personas.phrolova import CORE_PROMPT, EXAMPLES, RELATIONSHIPS  # noqa: E402


def training_rows():
    for example in EXAMPLES:
        yield {"messages": [
            {"role": "system", "content": CORE_PROMPT + "\n" + RELATIONSHIPS[example["relationship"]]},
            {"role": "user", "content": example["user"]},
            {"role": "assistant", "content": example["assistant"]},
        ]}


def export(output_dir: Path, *, force: bool = False):
    output_dir = Path(output_dir)
    targets = {"corpus.jsonl": EXAMPLES, "train.jsonl": training_rows()}
    # Check both paths before writing either file. Existing files need explicit opt-in.
    for name in targets:
        path = output_dir / name
        if path.is_symlink() or (path.exists() and (not force or not path.is_file())):
            raise FileExistsError(f"{path}: already exists; use --force to replace generated files")
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in targets.items():
        with (output_dir / name).open("w" if force else "x", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    return [output_dir / name for name in targets]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "personas/phrolova/generated")
    parser.add_argument("--force", action="store_true", help="Replace existing generated files")
    args = parser.parse_args()
    try:
        paths = export(args.output_dir, force=args.force)
    except (OSError, KeyError) as error:
        parser.exit(1, f"EXPORT FAILED: {error}\n")
    print(f"EXPORT PASS examples={len(EXAMPLES)} files={len(paths)} output={args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
