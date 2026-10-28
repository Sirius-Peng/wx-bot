'use strict';
// Production is cloud-only. Historical model validators and research artefacts are
// deliberately outside this resource gate; never download or remove them here.
const fs = require('node:fs');
const path = require('node:path');

const RETIRED = ['onnxruntime', 'onnx', 'tokenizers', 'numpy', 'torch', 'transformers'];
const WORKERS = ['src.core.local_decision', 'src.core.local_decision_worker', 'src.core.local_decision_cpu'];
const fail = reason => { throw new Error(`RUNTIME_RESOURCES FAIL: ${reason}`); };
const read = (root, name) => fs.readFileSync(path.join(root, name), 'utf8').replace(/^\uFEFF/, '');
const uncomment = source => source.split('\n').filter(line => !line.trimStart().startsWith('#')).join('\n');

function verifyRuntimeResources(root = path.resolve(__dirname, '../..'), {backendRequired = false, scanBackend = true} = {}) {
  const builder = uncomment(read(root, 'desktop/electron-builder.yml'));
  if (/decision[-_]model|verify-decision-model/.test(builder)) fail('retired_model_resource');
  if (!builder.includes('beforePack: ./scripts/verify-runtime-resources.cjs')) fail('missing_resource_hook');
  const requirements = uncomment(read(root, 'requirements.txt'));
  for (const dependency of RETIRED) {
    if (new RegExp(`^${dependency}(?:\\[|[<>=!~;\\s]|$)`, 'mi').test(requirements)) fail(`retired_dependency:${dependency}`);
  }
  const shell = read(root, 'desktop/scripts/build-backend.sh');
  const ps = read(root, 'desktop/scripts/build-backend.ps1');
  for (const module of [...WORKERS, ...RETIRED]) {
    if (!shell.includes(`--exclude-module ${module} \\`) || !ps.includes(`'${module}'`) ||
        !ps.includes("$arguments += @('--exclude-module',$module)")) fail(`missing_build_exclusion:${module}`);
    if (shell.includes(`--collect-all ${module} `) || shell.includes(`--hidden-import ${module} `)) fail(`retired_build_collection:${module}`);
  }
  // A quoted module in the PS exclusion list is allowed; reject it in either
  // collection loop even if someone leaves the correct exclusion in place.
  for (const match of ps.matchAll(/foreach\s*\(\$module\s+in\s+@\(([\s\S]*?)\)\)\s*\{([\s\S]*?)\}/g)) {
    if (match[2].includes('--collect-all') || match[2].includes('--hidden-import')) {
      for (const module of [...WORKERS, ...RETIRED]) if (match[1].includes(`'${module}'`)) fail(`retired_build_collection:${module}`);
    }
  }
  const entry = read(root, 'desktop/backend/entry.py');
  if (entry.includes('--local-decision-worker') || entry.includes('src.core.local_decision')) fail('retired_worker_entry');
  if (read(root, 'desktop/src/main/backend.js').includes('WXBOT_LOCAL_DECISION_DIR:')) fail('retired_model_environment');
  const pkg = JSON.parse(read(root, 'desktop/package.json'));
  for (const name of ['pack:mac', 'pack:mac:x64', 'pack:win', 'pack:all']) {
    if (!pkg.scripts[name]?.startsWith('npm run verify:resources && ') || pkg.scripts[name].includes('verify:model')) fail(`missing_pack_guard:${name}`);
  }
  if (pkg.scripts.prepack !== 'npm run verify:resources') fail('missing_prepack_guard');

  const backend = path.join(root, 'desktop/resources/backend');
  let scanned = 0;
  const isRetired = name => /\.(?:onnx|safetensors)$/i.test(name) || /^(?:onnxruntime|onnx|tokenizers|numpy|torch|transformers|safetensors)(?:$|[._-])/i.test(name);
  const scan = folder => {
    for (const entry of fs.readdirSync(folder, {withFileTypes: true})) {
      const filename = path.join(folder, entry.name);
      if (entry.isSymbolicLink()) fail(`backend_symlink:${path.relative(backend, filename)}`);
      if (isRetired(entry.name)) fail(`retired_backend_resource:${path.relative(backend, filename)}`);
      if (entry.isDirectory()) scan(filename);
      else if (entry.isFile()) scanned++;
    }
  };
  // Pre-build checks do not inspect the previous backend: the native build
  // scripts replace their own output before freezing the new cloud-only backend.
  // The direct electron-builder hook always scans the actual copied resources.
  if (scanBackend && fs.existsSync(backend)) scan(backend);
  const executables = ['wxbot-server', 'wxbot-server.exe', 'wxbot-server/wxbot-server.exe'];
  if (backendRequired && !executables.some(name => {
    const filename = path.join(backend, name);
    return fs.existsSync(filename) && fs.statSync(filename).isFile() && fs.statSync(filename).size > 0;
  })) fail('backend_missing_build_first');
  return {decision: 'cloud', modelRequired: false, backendFilesScanned: scanned, backendRequired, scanBackend};
}

module.exports = async (context) => {
  const result = verifyRuntimeResources(undefined, {backendRequired: true});
  if (context?.electronPlatformName === 'win32') require('./prepare-napcat.cjs').verify();
  console.log(`RUNTIME_RESOURCES PASS: decision=cloud; model_required=false; backend_files_scanned=${result.backendFilesScanned}`);
};
module.exports.verifyRuntimeResources = verifyRuntimeResources;
if (require.main === module) {
  try {
    const sourceOnly = process.argv.includes('--source-only');
    const result = verifyRuntimeResources(undefined, {backendRequired: !sourceOnly, scanBackend: !sourceOnly});
    console.log(`RUNTIME_RESOURCES PASS: decision=cloud; model_required=false; backend_files_scanned=${result.backendFilesScanned}; backend_required=${result.backendRequired}`);
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
