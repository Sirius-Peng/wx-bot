'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {verifyRuntimeResources} = require('../desktop/scripts/verify-runtime-resources.cjs');
const root = path.resolve(__dirname, '..');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'wxbot-cloud-package-'));
const sources = ['desktop/electron-builder.yml', 'requirements.txt', 'desktop/scripts/build-backend.sh',
  'desktop/scripts/build-backend.ps1', 'desktop/backend/entry.py', 'desktop/src/main/backend.js', 'desktop/package.json'];
let tests = 0;
const write = (name, value) => {
  const filename = path.join(temp, name); fs.mkdirSync(path.dirname(filename), {recursive: true}); fs.writeFileSync(filename, value);
};
const get = name => fs.readFileSync(path.join(temp, name), 'utf8');
const reset = () => {
  fs.rmSync(temp, {recursive: true, force: true}); fs.mkdirSync(temp);
  for (const name of sources) write(name, fs.readFileSync(path.join(root, name)));
};
const rejects = (mutate, reason, options = {}) => {
  reset(); mutate(); assert.throws(() => verifyRuntimeResources(temp, options), reason); tests++;
};
try {
  reset(); const result = verifyRuntimeResources(temp);
  assert.equal(result.decision, 'cloud'); assert.equal(result.modelRequired, false); assert.equal(result.backendFilesScanned, 0); tests++;
  // No offline model is needed even for a formal resource check.
  write('desktop/resources/backend/wxbot-server/wxbot-server.exe', 'nonempty synthetic backend fixture');
  assert.equal(verifyRuntimeResources(temp, {backendRequired: true}).backendFilesScanned, 1); tests++;
  // External research artefacts remain untouched, and are not copied by builder.
  write('desktop/resources/decision-model/model.onnx', 'research-do-not-package');
  verifyRuntimeResources(temp, {backendRequired: true});
  assert.equal(get('desktop/resources/decision-model/model.onnx'), 'research-do-not-package'); tests++;
  rejects(() => {}, /backend_missing_build_first/, {backendRequired: true});
  reset(); write('desktop/resources/backend/_internal/onnxruntime/old.dll', 'obsolete previous build');
  assert.equal(verifyRuntimeResources(temp, {scanBackend: false}).backendFilesScanned, 0); tests++;
  assert.equal(get('desktop/resources/backend/_internal/onnxruntime/old.dll'), 'obsolete previous build'); tests++;
  for (const dependency of ['onnxruntime', 'onnx', 'tokenizers', 'numpy', 'torch', 'transformers']) {
    rejects(() => write('requirements.txt', get('requirements.txt') + `\n${dependency}==1.0\n`), /retired_dependency/);
    rejects(() => write(`desktop/resources/backend/_internal/${dependency}/__init__.py`, ''), /retired_backend_resource/);
  }
  for (const name of ['old-model.onnx', 'task-head.safetensors', 'onnxruntime.dll', 'numpy.libs']) {
    rejects(() => write(`desktop/resources/backend/${name}`, 'old resource'), /retired_backend_resource/);
  }
  rejects(() => write('desktop/electron-builder.yml', get('desktop/electron-builder.yml') + '\n  - from: resources/decision-model\n    to: decision-model\n'), /retired_model_resource/);
  rejects(() => write('desktop/scripts/build-backend.sh', get('desktop/scripts/build-backend.sh').replace('--exclude-module onnxruntime \\', '--collect-all onnxruntime \\')), /missing_build_exclusion|retired_build_collection/);
  rejects(() => write('desktop/scripts/build-backend.ps1', get('desktop/scripts/build-backend.ps1').replace("'tzdata','jieba'", "'tzdata','jieba','onnxruntime'")), /retired_build_collection/);
  rejects(() => write('desktop/backend/entry.py', get('desktop/backend/entry.py') + '\n# --local-decision-worker\n'), /retired_worker_entry/);
  rejects(() => write('desktop/src/main/backend.js', get('desktop/src/main/backend.js') + '\nWXBOT_LOCAL_DECISION_DIR: oldPath\n'), /retired_model_environment/);
  rejects(() => {
    const pkg = JSON.parse(get('desktop/package.json')); pkg.scripts['pack:win'] = 'npm run verify:model && electron-builder --win';
    write('desktop/package.json', JSON.stringify(pkg));
  }, /missing_pack_guard/);
  // Verify no path/app identity drift accompanies retiring inference resources.
  const pkg = JSON.parse(fs.readFileSync(path.join(root, 'desktop/package.json'), 'utf8'));
  assert.equal(pkg.version, '3.0.0'); tests++;
  assert(fs.readFileSync(path.join(root, 'desktop/electron-builder.yml'), 'utf8').includes('appId: com.siriuspeng.wxbot')); tests++;
  verifyRuntimeResources(root); tests++;
  console.log(`CLOUD_PACKAGE PASS tests=${tests} missing_model=accepted model_assets=excluded training_dependencies=excluded user_data=unchanged`);
} finally { fs.rmSync(temp, {recursive: true, force: true}); }
