'use strict';
// These are deliberately tiny synthetic package fixtures, not accepted model assets.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const {verifyModel} = require('../desktop/scripts/verify-decision-model.cjs');
const root = path.join(__dirname, '..');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'wxbot-model-gate-'));
let tests = 0;
const write = (name, value) => fs.writeFileSync(path.join(temp, name), typeof value === 'string' ? value : JSON.stringify(value));
function fixture() {
  fs.rmSync(temp, {recursive: true, force: true}); fs.mkdirSync(temp);
  write('model.onnx', 'fixture-only-not-an-ONNX-model'); write('tokenizer.json', {fixture: true}); write('policy.json', {fixture: true});
  write('calibration.json', {fitted_on_independent_split: true, quantized: true, split_sha256: '1'.repeat(64)});
  const gate = {passed: true, checks: {independent_split: true, protected_zero_false_silence: true, silence_precision_99: true}};
  const manifest = {schema: 'wxbot.local-decision.v1', actions: ['reply', 'defer', 'context_only'],
    model_id: 'TEST-FIXTURE-ONLY', calibration: {fitted_on_independent_split: true}, quality_gate: gate,
    evaluation: {quality_gate: JSON.parse(JSON.stringify(gate)), split_sha256: '2'.repeat(64), warm_p95_ms: 250,
      metrics: {protected: 100, protected_missed: 0, silent_accepted: 100, silent_precision: .99, reply_records: 100, reply_recall: .995, coverage: .5}}, files: {}};
  for (const name of ['model.onnx', 'tokenizer.json', 'policy.json', 'calibration.json']) {
    manifest.files[name] = crypto.createHash('sha256').update(fs.readFileSync(path.join(temp, name))).digest('hex');
  }
  write('manifest.json', manifest); return manifest;
}
async function rejected(mutator, reason) {
  const manifest = fixture(); mutator(manifest); write('manifest.json', manifest);
  await assert.rejects(verifyModel(temp), reason); tests++;
}
async function main() {
  fixture(); assert.equal((await verifyModel(temp)).files, 4); tests++;
  await assert.rejects(verifyModel(path.join(temp, 'missing')), /model directory missing/); tests++;
  await rejected(m => { m.quality_gate.passed = 'true'; }, /gate_failed/);
  await rejected(m => { m.quality_gate.checks = {}; }, /gate_checks/);
  await rejected(m => { m.quality_gate.checks.independent_split = 1; }, /gate_checks/);
  await rejected(m => { m.quality_gate.checks.foo = 'true'; }, /gate_checks/);
  await rejected(m => { m.evaluation.quality_gate.passed = false; }, /gate_evaluation/);
  await rejected(m => { m.evaluation.quality_gate.passed = 1; }, /gate_evaluation/);
  await rejected(m => { m.evaluation.metrics = {}; }, /gate_metrics/);
  await rejected(m => { m.evaluation.metrics.silent_accepted = 0; }, /gate_metrics/);
  await rejected(m => { m.evaluation.metrics.silent_precision = .989; }, /gate_metrics/);
  await rejected(m => { m.evaluation.metrics.reply_recall = .994; }, /gate_metrics/);
  await rejected(m => { m.evaluation.metrics.reply_records = 99; }, /gate_metrics/);
  await rejected(m => { m.evaluation.metrics.protected_missed = 1; }, /gate_metrics/);
  await rejected(m => { m.evaluation.metrics.protected = 99; }, /gate_metrics/);
  await rejected(m => { m.evaluation.metrics.coverage = .499; }, /gate_metrics/);
  await rejected(m => { m.evaluation.metrics.coverage = true; }, /gate_metrics/);
  await rejected(m => { m.evaluation.warm_p95_ms = 300.1; }, /gate_latency/);
  await rejected(m => { m.evaluation.split_sha256 = 'unknown'; }, /gate_split/);
  await rejected(m => { m.evaluation.split_sha256 = '1'.repeat(64); }, /uncalibrated/);
  await rejected(m => { m.calibration.fitted_on_independent_split = 'true'; }, /uncalibrated/);
  await rejected(m => { delete m.files['model.onnx']; }, /manifest_files/);
  await rejected(m => { m.files['model.onnx'] = 'f'.repeat(64); }, /manifest_hash/);
  await rejected(m => { m.files['../outside.onnx'] = '0'.repeat(64); }, /manifest_files/);
  await rejected(m => { m.files['C:\\outside'] = '0'.repeat(64); }, /manifest_files/);
  await rejected(m => { m.files['/tmp/x'] = '0'.repeat(64); }, /manifest_files/);
  await rejected(m => { m.files['model.onnx'] = 42; }, /manifest_files/);
  await rejected(() => { write('extra.onnx', 'unvalidated'); }, /manifest_untracked_file/);
  await rejected(() => { fs.unlinkSync(path.join(temp, 'model.onnx')); fs.symlinkSync(path.join(temp, 'tokenizer.json'), path.join(temp, 'model.onnx')); }, /manifest_path/);
  await rejected(() => { fs.unlinkSync(path.join(temp, 'tokenizer.json')); }, /manifest_path/);
  await rejected(m => {
    write('calibration.json', {fitted_on_independent_split: true, quantized: false, split_sha256: '1'.repeat(64)});
    m.files['calibration.json'] = crypto.createHash('sha256').update(fs.readFileSync(path.join(temp, 'calibration.json'))).digest('hex');
  }, /uncalibrated/);
  // This strict historical validator remains useful for research provenance,
  // but it is deliberately not a prerequisite for cloud-only production packs.
  console.log(`RESEARCH_MODEL_VALIDATOR PASS tests=${tests} hashes=verified gate_flags=strict metrics=rechecked production_gate=false`);
}
main().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => fs.rmSync(temp, {recursive: true, force: true}));
