'use strict';

// Build-time preparation only. Never executes NapCat, QQ, installers or Docker.
// WXBOT_NAPCAT_MIRROR is an optional full HTTPS download URL; the pinned SHA,
// release, size, provenance and license checks do not change with the mirror.
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const http = require('http');
const https = require('https');

function freeze(value) {
  if (value && typeof value === 'object') {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}
const DEFAULT_MANIFEST = freeze(require('../napcat-runtime.json'));
const DEFAULT_DIR = path.resolve(__dirname, '../resources/napcat');
const digest = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
function fileName(value) {
  return typeof value === 'string' && value.length > 0 && value !== '.' && value !== '..'
    && !/[\\/\0]/.test(value) && !/^[A-Za-z]:/.test(value);
}
function options(input = {}) {
  const manifest = input.manifest || DEFAULT_MANIFEST;
  const asset = manifest.asset;
  if (!manifest.version || !asset || !fileName(asset.name) || !digest(asset.sha256)
      || !Number.isSafeInteger(asset.bytes) || asset.bytes < 1 || asset.bytes > 512 * 1024 * 1024
      || !fileName(manifest.provenance_file) || !Array.isArray(manifest.licenses)
      || manifest.licenses.length !== 2 || manifest.licenses.some(item => !fileName(item.file) || !digest(item.sha256))) {
    throw new Error('Invalid pinned NapCat manifest');
  }
  const timeoutMs = input.timeoutMs === undefined ? 600000 : input.timeoutMs;
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 1800000) throw new Error('Invalid NapCat download timeout');
  return { ...input, manifest, timeoutMs, resourceDir: path.resolve(input.resourceDir || DEFAULT_DIR) };
}
function regular(file, maxBytes) {
  const st = fs.lstatSync(file);
  if (!st.isFile() || st.isSymbolicLink() || st.size > maxBytes) throw new Error('NapCat resource must be a bounded regular file: ' + path.basename(file));
  return st;
}
function hashFile(file, maxBytes) {
  regular(file, maxBytes);
  const fd = fs.openSync(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
  try {
    const hash = crypto.createHash('sha256'), buffer = Buffer.alloc(65536);
    let count = 0, n;
    while ((n = fs.readSync(fd, buffer, 0, buffer.length, null))) {
      count += n;
      if (count > maxBytes) throw new Error('NapCat resource grew beyond its pinned limit');
      hash.update(buffer.subarray(0, n));
    }
    return { bytes: count, sha256: hash.digest('hex') };
  } finally { fs.closeSync(fd); }
}
function verifyMetadata(o) {
  const { manifest: m, resourceDir: dir } = o;
  const source = path.join(dir, m.provenance_file);
  regular(source, 65536);
  const provenance = JSON.parse(fs.readFileSync(source, 'utf8'));
  for (const field of ['component', 'version', 'source_commit', 'source', 'release']) {
    if (provenance[field] !== m[field]) throw new Error('NapCat provenance mismatch: ' + field);
  }
  for (const field of ['name', 'url', 'bytes', 'sha256', 'members', 'unpacked_bytes']) {
    if (provenance.asset?.[field] !== m.asset[field]) throw new Error('NapCat asset provenance mismatch: ' + field);
  }
  for (const item of m.licenses) {
    const declared = provenance.licenses?.find(value => value.file === item.file);
    if (!declared || declared.sha256 !== item.sha256 || declared.source !== item.source) throw new Error('NapCat license provenance mismatch');
    if (hashFile(path.join(dir, item.file), 2 * 1024 * 1024).sha256 !== item.sha256) throw new Error('NapCat license SHA256 mismatch: ' + item.file);
  }
}
function verifyArchive(o) {
  const { manifest: m, resourceDir: dir } = o;
  const actual = hashFile(path.join(dir, m.asset.name), m.asset.bytes);
  if (actual.bytes !== m.asset.bytes || actual.sha256 !== m.asset.sha256) throw new Error('NapCat ZIP size/SHA256 mismatch');
  return { ok: true, version: m.version, archive: path.join(dir, m.asset.name), ...actual };
}
function verify(input) {
  const o = options(input);
  verifyMetadata(o);
  return verifyArchive(o);
}
function validUrl(value, o) {
  let url;
  try { url = new URL(value); } catch { throw new Error('Invalid NapCat download URL'); }
  const fixture = o.manifest !== DEFAULT_MANIFEST && o.allowHttpLoopback === true
    && url.protocol === 'http:' && ['127.0.0.1', '[::1]', 'localhost'].includes(url.hostname);
  if ((url.protocol !== 'https:' && !fixture) || url.username || url.password || url.hash) throw new Error('NapCat downloads require HTTPS');
  return url;
}
function getResponse(url, signal) {
  return new Promise((resolve, reject) => {
    const transport = url.protocol === 'https:' ? https : http;
    const request = transport.get(url, { signal, headers: { 'User-Agent': 'wxbot-pinned-napcat-builder', 'Accept-Encoding': 'identity' } }, resolve);
    request.once('error', reject);
  });
}
async function responseAt(value, o, signal) {
  let url = validUrl(value, o);
  for (let count = 0; count <= 4; count++) {
    const response = await getResponse(url, signal);
    if ([301, 302, 303, 307, 308].includes(response.statusCode)) {
      const location = response.headers.location;
      response.destroy();
      if (count === 4 || !location) throw new Error('NapCat download redirect limit exceeded');
      url = validUrl(new URL(location, url).href, o);
      continue;
    }
    if (response.statusCode !== 200) {
      response.destroy();
      throw new Error('NapCat download HTTP ' + response.statusCode);
    }
    return response;
  }
  throw new Error('NapCat download redirect limit exceeded');
}
function writeBody(response, fd, expected) {
  return new Promise((resolve, reject) => {
    let received = 0, finished = false;
    const hash = crypto.createHash('sha256');
    function done(error) {
      if (finished) return;
      finished = true;
      response.removeListener('readable', pump);
      if (error) { response.destroy(); reject(error); }
      else resolve({ bytes: received, sha256: hash.digest('hex') });
    }
    function pump() {
      try {
        let chunk;
        // Consume at most pinned size + one sentinel byte, never an unbounded
        // response chunk or full in-memory ZIP. A sentinel aborts immediately.
        while (!finished && (chunk = response.read(Math.min(65536, expected + 1 - received))) !== null) {
          received += chunk.length;
          if (received > expected) return done(new Error('NapCat download exceeds pinned byte limit'));
          let offset = 0;
          while (offset < chunk.length) offset += fs.writeSync(fd, chunk, offset, chunk.length - offset);
          hash.update(chunk);
        }
      } catch (error) { done(error); }
    }
    response.on('readable', pump);
    response.once('error', done);
    response.once('aborted', () => done(new Error('NapCat download interrupted')));
    response.once('end', () => done(received === expected ? null : new Error('NapCat download incomplete')));
    response.once('close', () => { if (!finished) done(new Error('NapCat download closed before completion')); });
    pump();
  });
}
async function prepare(input) {
  const o = options(input);
  verifyMetadata(o); // A network failure never replaces original licenses/source.
  const final = path.join(o.resourceDir, o.manifest.asset.name);
  if (fs.existsSync(final)) return { ...verifyArchive(o), downloaded: false };
  const url = o.mirrorUrl || process.env.WXBOT_NAPCAT_MIRROR || o.manifest.asset.url;
  validUrl(url, o); // Reject insecure overrides before creating any partial file.
  const partial = final + '.' + process.pid + '.' + crypto.randomBytes(8).toString('hex') + '.part';
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), o.timeoutMs);
  let fd;
  try {
    fd = fs.openSync(partial, fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY, 0o600);
    const response = await responseAt(url, o, controller.signal);
    const length = response.headers['content-length'];
    if ((length !== undefined && (!/^\d+$/.test(length) || Number(length) !== o.manifest.asset.bytes))
        || (response.headers['content-encoding'] && response.headers['content-encoding'] !== 'identity')) {
      response.destroy(); throw new Error('NapCat download content length/encoding mismatch');
    }
    const result = await writeBody(response, fd, o.manifest.asset.bytes);
    if (result.sha256 !== o.manifest.asset.sha256) throw new Error('NapCat download SHA256 mismatch');
    fs.fsyncSync(fd); fs.closeSync(fd); fd = undefined;
    verifyMetadata(o);
    // Do not overwrite a file another preparer/user created while downloading.
    if (fs.existsSync(final)) return { ...verifyArchive(o), downloaded: false };
    fs.renameSync(partial, final);
    return { ...verifyArchive(o), downloaded: true };
  } finally {
    clearTimeout(timer); controller.abort();
    if (fd !== undefined) fs.closeSync(fd);
    fs.rmSync(partial, { force: true });
  }
}

module.exports = { DEFAULT_MANIFEST, verify, prepare };
if (require.main === module) {
  const args = process.argv.slice(2);
  if (args.some(arg => arg !== '--verify')) {
    process.stderr.write('Usage: node prepare-napcat.cjs [--verify]\n'); process.exitCode = 1;
  } else {
    Promise.resolve().then(() => args.includes('--verify') ? verify() : prepare()).then(result => {
      process.stdout.write('NAPCAT_BUNDLE PASS version=' + result.version + ' bytes=' + result.bytes + ' SHA256=' + result.sha256 + '\n');
    }).catch(error => { process.stderr.write('NAPCAT_BUNDLE FAIL: ' + error.message + '\n'); process.exitCode = 1; });
  }
}
