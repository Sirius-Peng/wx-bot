"""No-model tests: training augmentation cannot rewrite or impersonate blind data."""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from src.core.local_decision_worker import state_text
from tools.decision_model.corpus import prepare as prepare_source
from tools.decision_model.curriculum import (
    COUNTERFACTUALS, SITUATIONS, VERSION, _normalized, incremental_rows, prepare,
)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class CurriculumExamples(unittest.TestCase):
    def test_substantive_situations_have_distinct_events_constraints_and_outcomes(self):
        self.assertEqual(len(SITUATIONS), 60)
        for column in range(6):
            self.assertEqual(len({s[column] for s in SITUATIONS}), 60)
        self.assertTrue(all(len(s[2]) >= 11 and len(s[3]) >= 8 for s in SITUATIONS))

    def test_expected_balanced_training_increment_not_independent_annotation_claim(self):
        rows = incremental_rows()
        self.assertEqual(len(rows), 1820)
        self.assertEqual(Counter(r['label'] for r in rows), {'reply': 610, 'defer': 604, 'context_only': 606})
        self.assertEqual(len({r['family'] for r in rows}), 70)
        self.assertTrue(all(r['provenance']['human_labelled'] is False for r in rows))
        self.assertTrue(all(r['provenance']['purpose'] == 'training_only' for r in rows))
        self.assertTrue(all(r['provenance']['curriculum_version'] == VERSION for r in rows))

    def test_every_record_has_recent_user_context_and_unique_id_and_full_state(self):
        rows = incremental_rows()
        self.assertTrue(all(r['state']['recent'] for r in rows))
        self.assertEqual(len({r['id'] for r in rows}), len(rows))
        self.assertEqual(len({json.dumps(r['state'], sort_keys=True) for r in rows}), len(rows))
        self.assertTrue(all(r['protected'] == (r['label'] == 'reply') for r in rows))

    def test_scope_counterfactuals_keep_text_but_change_history_and_expected_action(self):
        grouped = defaultdict(list)
        for row in incremental_rows():
            if row['slice'] == 'history_counterfactual':
                grouped[row['family']].append(row)
        self.assertEqual(len(grouped), len(COUNTERFACTUALS))
        for rows in grouped.values():
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['state']['current'], rows[1]['state']['current'])
            self.assertNotEqual(rows[0]['label'], rows[1]['label'])
            self.assertNotEqual(state_text(rows[0]['state']), state_text(rows[1]['state']))
            self.assertEqual(rows[0]['session_id'], rows[1]['session_id'])

    def test_requested_slices_are_present_not_only_punctuation_variants(self):
        counts = Counter(r['slice'] for r in incremental_rows())
        for name in ('oral_request', 'emotion', 'correction', 'quoted_boundary',
                     'irony', 'withdrawal', 'quote_not_request', 'cancel_request',
                     'explicit_pause', 'topic_closed', 'pending_comparison'):
            self.assertEqual(counts[name], 60)
        for name in ('traditional', 'mixed_language', 'merged_envelopes'):
            self.assertEqual(counts[name], 180)
        self.assertEqual(sum('\n' in r['state']['current'] for r in incremental_rows()), 180)

    def test_generation_is_reproducible_and_seed_changes_only_order(self):
        self.assertEqual(incremental_rows(), incremental_rows())
        self.assertNotEqual(incremental_rows(1), incremental_rows(2))
        self.assertEqual({r['id'] for r in incremental_rows(1)}, {r['id'] for r in incremental_rows(2)})

    def test_no_normalized_current_overlap_with_frozen_stress_without_reading_predictions(self):
        # Equality guard only: no labels, predictions or errors enter the curriculum.
        path = Path(__file__).resolve().parents[1] / 'tools/decision_model/stress-v1.jsonl'
        before = path.read_bytes()
        locked_text = {_normalized(json.loads(line)['state']['current']) for line in before.decode().splitlines()}
        train_text = {_normalized(row['state']['current']) for row in incremental_rows()}
        self.assertFalse(train_text.intersection(locked_text))
        self.assertEqual(path.read_bytes(), before)


class CurriculumStorage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.out = self.root / 'new-data'
        prepare_source(self.source, per_split=(90, 90, 90))

    def source_hashes(self):
        return {p.name: digest(p.read_bytes()) for p in self.source.iterdir() if p.is_file()}

    def replace_split(self, split, rows):
        raw = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows).encode()
        (self.source / (split + '.jsonl')).write_bytes(raw)
        meta = json.loads((self.source / 'dataset.json').read_text())
        meta['splits'][split]['sha256'] = digest(raw)
        meta['splits'][split]['records'] = len(rows)
        (self.source / 'dataset.json').write_text(json.dumps(meta))

    def test_reopens_output_and_preserves_source_and_development_holdouts_exactly(self):
        before = self.source_hashes()
        meta = prepare(self.source, self.out)
        self.assertEqual(self.source_hashes(), before)
        for split in ('calibration', 'test'):
            source = (self.source / (split + '.jsonl')).read_bytes()
            copied = (self.out / (split + '.jsonl')).read_bytes()
            self.assertEqual(copied, source)
            self.assertEqual(meta['splits'][split]['sha256'], digest(source))
            self.assertEqual(meta['splits'][split]['role'], 'seen_development_regression')
        self.assertEqual(meta['splits']['train']['records'], 1910)
        self.assertEqual(meta['increment']['records'], 1820)
        self.assertEqual(meta['increment']['independent_natural_conversations'], 0)
        self.assertFalse(meta['release_acceptance_eligible'])
        self.assertEqual((self.out / 'source-dataset.json').read_bytes(), (self.source / 'dataset.json').read_bytes())
        self.assertEqual(json.loads((self.out / 'dataset.json').read_text()), meta)
        self.assertEqual(meta['splits']['train']['sha256'], digest((self.out / 'train.jsonl').read_bytes()))

    def test_all_new_families_and_sessions_are_train_only(self):
        meta = prepare(self.source, self.out)
        new_families = set(meta['increment']['families'])
        heldout_families = {family for split in ('calibration', 'test') for family in meta['splits'][split]['families']}
        self.assertFalse(new_families.intersection(heldout_families))
        increments = [json.loads(line) for line in (self.out / 'increment.jsonl').read_text().splitlines()]
        training = [json.loads(line) for line in (self.out / 'train.jsonl').read_text().splitlines()]
        self.assertTrue({r['id'] for r in increments}.issubset({r['id'] for r in training}))
        heldout_texts = {_normalized(json.loads(line)['state']['current']) for split in ('calibration', 'test')
                        for line in (self.out / (split + '.jsonl')).read_text().splitlines()}
        self.assertFalse({_normalized(r['state']['current']) for r in increments}.intersection(heldout_texts))

    def test_source_train_can_be_sampled_but_not_mutated(self):
        before = self.source_hashes()
        meta = prepare(self.source, self.out, retain_source_train=21)
        self.assertEqual(meta['retained_source_train_records'], 21)
        self.assertEqual(meta['splits']['train']['records'], 1841)
        self.assertEqual(self.source_hashes(), before)

    def test_same_seed_and_source_give_same_training_bytes_at_new_locations(self):
        a = prepare(self.source, self.out)
        second = self.root / 'new-data-second'
        b = prepare(self.source, second)
        self.assertEqual(a, b)
        self.assertEqual((self.out / 'train.jsonl').read_bytes(), (second / 'train.jsonl').read_bytes())

    def test_crlf_heldouts_are_copied_as_bytes_without_line_ending_rewrite(self):
        meta = json.loads((self.source / 'dataset.json').read_text())
        for split in ('calibration', 'test'):
            path = self.source / (split + '.jsonl')
            raw = path.read_bytes().replace(b'\n', b'\r\n')
            path.write_bytes(raw)
            meta['splits'][split]['sha256'] = digest(raw)
        (self.source / 'dataset.json').write_text(json.dumps(meta))
        prepare(self.source, self.out)
        self.assertEqual((self.out / 'calibration.jsonl').read_bytes(), (self.source / 'calibration.jsonl').read_bytes())

    def test_existing_output_is_not_overwritten(self):
        self.out.mkdir()
        marker = self.out / 'mine'
        marker.write_text('keep this')
        with self.assertRaisesRegex(ValueError, 'output_exists'):
            prepare(self.source, self.out)
        self.assertEqual(marker.read_text(), 'keep this')

    def test_nested_output_is_rejected_without_changing_source(self):
        before = self.source_hashes()
        with self.assertRaisesRegex(ValueError, 'separate'):
            prepare(self.source, self.source / 'nested')
        self.assertEqual(before, self.source_hashes())

    def test_wrong_source_hash_is_not_declared_as_new_frozen_data(self):
        path = self.source / 'test.jsonl'
        path.write_bytes(path.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'source_manifest_mismatch:test'):
            prepare(self.source, self.out)
        self.assertFalse(self.out.exists())

    def test_missing_source_manifest_is_rejected(self):
        (self.source / 'dataset.json').unlink()
        with self.assertRaisesRegex(ValueError, 'source_dataset_manifest_required'):
            prepare(self.source, self.out)
        self.assertFalse(self.out.exists())

    def test_heldout_overlap_aborts_instead_of_silently_removing_a_hard_case(self):
        overlap = incremental_rows()[0]
        self.replace_split('calibration', [overlap])
        before = self.source_hashes()
        with self.assertRaisesRegex(ValueError, 'overlaps_heldout_source'):
            prepare(self.source, self.out)
        self.assertFalse(self.out.exists())
        self.assertEqual(before, self.source_hashes())

    def test_namespace_collision_aborts_without_overwriting_source(self):
        row = json.loads((self.source / 'train.jsonl').read_text().splitlines()[0])
        row['id'] = incremental_rows()[0]['id']
        self.replace_split('train', [row])
        before = self.source_hashes()
        with self.assertRaisesRegex(ValueError, 'namespace_collision'):
            prepare(self.source, self.out)
        self.assertFalse(self.out.exists())
        self.assertEqual(before, self.source_hashes())

    def test_known_blind_source_is_rejected_before_any_split_is_read(self):
        from unittest.mock import patch
        path = self.source / 'dataset.json'
        original = json.loads(path.read_text())
        cases = [
            {'release_acceptance_eligible': True},
            {'evaluation_role': 'locked_blind_acceptance'},
            {'splits': {'test': {'role': 'locked_blind_acceptance'}}},
        ]
        for declared in cases:
            with self.subTest(declared=declared):
                path.write_text(json.dumps({**original, **declared}))
                with patch('tools.decision_model.curriculum._read_jsonl', side_effect=AssertionError('blind rows read')):
                    with self.assertRaisesRegex(ValueError, 'source_blind_acceptance_not_allowed'):
                        prepare(self.source, self.out)
                self.assertFalse(self.out.exists())

    def test_invalid_sample_count_is_rejected(self):
        for count in (-1, True, 1.5):
            with self.assertRaisesRegex(ValueError, 'invalid_retain_source_train'):
                prepare(self.source, self.out, retain_source_train=count)
        self.assertFalse(self.out.exists())


if __name__ == '__main__':
    unittest.main()
