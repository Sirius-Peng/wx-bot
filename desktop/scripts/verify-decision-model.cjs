'use strict';
// Historical research-package validation only; not invoked by production builds.
// No model download or environment-path override. Local inference is retired.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {isDeepStrictEqual} = require('node:util');

const ACTIONS = ['reply', 'defer', 'context_only'];
const SHA256 = /^[0-9a-f]{64}$/;
const fail = reason => { throw new Error(`LOCAL_DECISION_PACKAGE FAIL: ${reason}`); };
const finite = value => typeof value === 'number' && Number.isFinite(value);
const object = value => value && typeof value === 'object' && !Array.isArray(value);
const readJson = filename => {
  try { return JSON.parse(fs.readFileSync(filename, 'utf8')); }
  catch (_) { return fail(`missing or invalid JSON: ${path.basename(filename)}`); }
};

function verifyGate(manifest) {
  const gate = manifest.quality_gate;
  if (!object(gate) || gate.passed !== true) fail('gate_failed');
  if (!object(gate.checks) || !Object.keys(gate.checks).length ||
      Object.values(gate.checks).some(value => value !== true) || gate.checks.independent_split !== true) fail('gate_checks');
  const evaluation = manifest.evaluation;
  if (!object(evaluation) || !isDeepStrictEqual(evaluation.quality_gate, gate)) fail('gate_evaluation');
  const metrics = evaluation.metrics;
  if (!object(metrics)) fail('gate_metrics');
  const rules = {
    protected: value => finite(value) && value >= 100,
    protected_missed: value => finite(value) && value === 0,
    silent_accepted: value => finite(value) && value >= 100,
    silent_precision: value => finite(value) && value >= .99 && value <= 1,
    reply_records: value => finite(value) && value >= 100,
    reply_recall: value => finite(value) && value >= .995 && value <= 1,
    coverage: value => finite(value) && value >= .5 && value <= 1,
  };
  if (Object.entries(rules).some(([name, check]) => !check(metrics[name]))) fail('gate_metrics');
  if (!finite(evaluation.warm_p95_ms) || evaluation.warm_p95_ms <= 0 || evaluation.warm_p95_ms > 300) fail('gate_latency');
  if (typeof evaluation.split_sha256 !== 'string' || !SHA256.test(evaluation.split_sha256)) fail('gate_split');
}

async function digest(filename) {
  const hash = crypto.createHash('sha256');
  // A production INT8 model is large; avoid a second whole-model memory allocation.
  for await (const chunk of fs.createReadStream(filename)) hash.update(chunk);
  return hash.digest('hex');
}

async function verifyModel(folder = path.resolve(__dirname, '../resources/decision-model')) {
  const root = path.resolve(folder);
  if (!fs.existsSync(root) || !fs.statSync(root).isDirectory()) fail('model directory missing');
  const realRoot = fs.realpathSync(root);
  const manifestPath = path.join(root, 'manifest.json');
  if (!fs.existsSync(manifestPath) || fs.lstatSync(manifestPath).isSymbolicLink()) fail('manifest_path');
  const manifest = readJson(manifestPath);
  if (!object(manifest) || manifest.schema !== 'wxbot.local-decision.v1' ||
      !isDeepStrictEqual(manifest.actions, ACTIONS)) fail('manifest_schema');
  if (!object(manifest.calibration) || manifest.calibration.fitted_on_independent_split !== true) fail('uncalibrated');
  verifyGate(manifest);
  const files = manifest.files;
  if (!object(files) || !['model.onnx', 'tokenizer.json', 'policy.json', 'calibration.json'].every(name => Object.hasOwn(files, name))) fail('manifest_files');
  for (const [relative, expected] of Object.entries(files)) {
    if (!relative || /[\\:\0]/.test(relative) || relative.split('/').some(part => !part || part === '.' || part === '..') ||
        typeof expected !== 'string' || !SHA256.test(expected)) fail('manifest_files');
    const filename = path.join(root, relative);
    let cursor = root;
    for (const segment of relative.split('/')) {
      cursor = path.join(cursor, segment);
      if (!fs.existsSync(cursor) || fs.lstatSync(cursor).isSymbolicLink()) fail('manifest_path');
    }
    const resolved = fs.realpathSync(filename);
    const containment = path.relative(realRoot, resolved);
    if (containment.startsWith(`..${path.sep}`) || path.isAbsolute(containment) ||
        !fs.statSync(filename).isFile() || fs.statSync(filename).size === 0) fail('manifest_path');
    if (await digest(filename) !== expected) fail('manifest_hash');
  }
  function validateDirectory(directory) {
    for (const entry of fs.readdirSync(directory, {withFileTypes: true})) {
      const filename = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) fail('manifest_untracked_file');
      if (entry.isDirectory()) validateDirectory(filename);
      else if (filename !== manifestPath && !Object.hasOwn(files, path.relative(root, filename).split(path.sep).join('/'))) fail('manifest_untracked_file');
    }
  }
  validateDirectory(root);
  const calibration = readJson(path.join(root, 'calibration.json'));
  if (!object(calibration) || calibration.fitted_on_independent_split !== true || calibration.quantized !== true ||
      typeof calibration.split_sha256 !== 'string' || !SHA256.test(calibration.split_sha256) ||
      calibration.split_sha256 === manifest.evaluation.split_sha256) fail('uncalibrated');
  return {root, modelId: manifest.model_id, files: Object.keys(files).length};
}

// Kept as a manual research CLI for authenticating previously trained artefacts.
module.exports = async () => {
  const result = await verifyModel();
  console.log(`LOCAL_DECISION_PACKAGE PASS: files=${result.files}; gate=passed; hashes=verified`);
};
module.exports.verifyModel = verifyModel;
module.exports.verifyGate = verifyGate;
if (require.main === module) {
  module.exports().catch(error => { console.error(error.message); process.exitCode = 1; });
}
