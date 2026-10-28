'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const crypto = require('node:crypto');
const { test } = require('node:test');
const { DEFAULT_MANIFEST, verify, prepare } = require('../desktop/scripts/prepare-napcat.cjs');
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
function fixture() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'napcat fixture 中文 '));
  const body = Buffer.from('small synthetic ZIP bytes; no binaries are executed');
  const manifest = structuredClone(DEFAULT_MANIFEST);
  manifest.asset = { name: 'fixture.zip', bytes: body.length, sha256: sha(body), url: 'https://example.invalid/pinned.zip', members: 1, unpacked_bytes: 100 };
  manifest.provenance_file = 'provenance.json';
  manifest.licenses = ['first', 'second'].map(name => ({ file: name + '-LICENSE', source: 'https://example.invalid/' + name, sha256: sha(name) }));
  for (const [i, license] of manifest.licenses.entries()) fs.writeFileSync(path.join(dir, license.file), ['first', 'second'][i]);
  const provenance = () => fs.writeFileSync(path.join(dir, manifest.provenance_file), JSON.stringify(manifest));
  provenance();
  return { dir, body, manifest, provenance, opts: { resourceDir: dir, manifest, allowHttpLoopback: true }, archive: path.join(dir, manifest.asset.name), close: () => fs.rmSync(dir, { force: true, recursive: true }) };
}
async function server(fn, run) {
  const s = http.createServer(fn);
  await new Promise(resolve => s.listen(0, '127.0.0.1', resolve));
  try { return await run('http://127.0.0.1:' + s.address().port); }
  finally { s.closeAllConnections(); await new Promise(resolve => s.close(resolve)); }
}
function partials(f) { return fs.readdirSync(f.dir).filter(name => name.endsWith('.part')); }

test('fixed production release, archive digest and two original licenses stay immutable', () => {
  assert.equal(DEFAULT_MANIFEST.version, '4.18.41');
  assert.equal(DEFAULT_MANIFEST.asset.bytes, 124089498);
  assert.equal(DEFAULT_MANIFEST.asset.sha256, '51fbb10e7bcd56b0c2a6de86c37811e7518330e48a137ac689ab1e4d8627d3f0');
  assert(DEFAULT_MANIFEST.asset.url.includes('/v4.18.41/'));
  assert.equal(DEFAULT_MANIFEST.licenses.length, 2);
  assert.equal(Object.keys(DEFAULT_MANIFEST.required_files).length, 7);
  assert(Object.isFrozen(DEFAULT_MANIFEST.asset));
});
test('verify is offline and exact; a valid existing archive is reused without HTTP', async () => {
  const f = fixture();
  try {
    fs.writeFileSync(f.archive, f.body);
    assert.equal(verify(f.opts).sha256, sha(f.body));
    const result = await prepare({ ...f.opts, mirrorUrl: 'https://must-not-contact.invalid/' });
    assert.equal(result.downloaded, false);
  } finally { f.close(); }
});
test('missing archive streams to a private partial then publishes only verified bytes', async () => {
  const f = fixture(); let calls = 0;
  try {
    await server((req, res) => { calls++; assert.equal(req.headers['accept-encoding'], 'identity'); res.end(f.body); }, async url => {
      const result = await prepare({ ...f.opts, mirrorUrl: url });
      assert.equal(result.downloaded, true); assert.deepEqual(fs.readFileSync(f.archive), f.body); assert.deepEqual(partials(f), []);
      assert.equal(verify(f.opts).ok, true);
    });
    assert.equal(calls, 1);
  } finally { f.close(); }
});
test('an existing damaged archive is never replaced or fetched', async () => {
  const f = fixture(); const old = Buffer.from('old user file'); fs.writeFileSync(f.archive, old);
  try {
    await assert.rejects(prepare({ ...f.opts, mirrorUrl: 'https://must-not-contact.invalid/' }), /SHA256|size/);
    assert.deepEqual(fs.readFileSync(f.archive), old); assert.deepEqual(partials(f), []);
  } finally { f.close(); }
});
for (const kind of ['license', 'provenance', 'source', 'asset']) test('metadata mismatch fails before downloading: ' + kind, async () => {
  const f = fixture();
  try {
    if (kind === 'license') fs.writeFileSync(path.join(f.dir, f.manifest.licenses[0].file), 'wrong');
    else {
      const p = JSON.parse(fs.readFileSync(path.join(f.dir, f.manifest.provenance_file)));
      if (kind === 'provenance') p.version = 'latest';
      if (kind === 'source') p.source_commit = 'wrong';
      if (kind === 'asset') p.asset.url = 'https://example.invalid/latest.zip';
      fs.writeFileSync(path.join(f.dir, f.manifest.provenance_file), JSON.stringify(p));
    }
    await assert.rejects(prepare(f.opts), /license|provenance/i);
    assert.equal(fs.existsSync(f.archive), false); assert.deepEqual(partials(f), []);
  } finally { f.close(); }
});
for (const kind of ['wrong-hash', 'short-body', 'size-plus-one', 'oversized-header', 'http-error', 'gzip']) test('bad response never publishes a file: ' + kind, async () => {
  const f = fixture();
  try {
    await server((req, res) => {
      if (kind === 'http-error') { res.writeHead(503); return res.end('no'); }
      if (kind === 'oversized-header') { res.setHeader('Content-Length', f.body.length + 1); return res.end(); }
      if (kind === 'gzip') res.setHeader('Content-Encoding', 'gzip');
      if (kind === 'wrong-hash') return res.end(Buffer.alloc(f.body.length, 1));
      if (kind === 'short-body') return res.end(f.body.subarray(0, 3));
      if (kind === 'size-plus-one') { res.write(f.body); return res.end('x'); }
      res.end(f.body);
    }, async url => { await assert.rejects(prepare({ ...f.opts, mirrorUrl: url })); });
    assert.equal(fs.existsSync(f.archive), false); assert.deepEqual(partials(f), []);
  } finally { f.close(); }
});
test('interrupted and timed-out downloads remove partials, leave metadata untouched', async () => {
  const f = fixture(); const before = fs.readFileSync(path.join(f.dir, f.manifest.provenance_file));
  try {
    await server((req, res) => { res.write(f.body.subarray(0, 2)); setTimeout(() => res.destroy(), 10); }, async url => {
      await assert.rejects(prepare({ ...f.opts, mirrorUrl: url }));
    });
    await server(() => {}, async url => { await assert.rejects(prepare({ ...f.opts, mirrorUrl: url, timeoutMs: 30 })); });
    assert.equal(fs.existsSync(f.archive), false); assert.deepEqual(partials(f), []);
    assert.deepEqual(fs.readFileSync(path.join(f.dir, f.manifest.provenance_file)), before);
  } finally { f.close(); }
});
test('redirects are bounded and their destinations are revalidated', async () => {
  const f = fixture();
  try {
    await server((req, res) => {
      if (req.url === '/file') return res.end(f.body);
      res.writeHead(302, { Location: req.url === '/start' ? '/file' : '/loop' }); res.end();
    }, async url => {
      assert.equal((await prepare({ ...f.opts, mirrorUrl: url + '/start' })).ok, true);
      fs.unlinkSync(f.archive);
      await assert.rejects(prepare({ ...f.opts, mirrorUrl: url + '/loop' }), /redirect/);
    });
    assert.deepEqual(partials(f), []);
  } finally { f.close(); }
});
test('production disallows HTTP; injected HTTP fixtures are limited to literal loopback', async () => {
  const f = fixture();
  try {
    await assert.rejects(prepare({ ...f.opts, allowHttpLoopback: false, mirrorUrl: 'http://127.0.0.1/file' }), /HTTPS/);
    await assert.rejects(prepare({ ...f.opts, mirrorUrl: 'http://example.invalid/file' }), /HTTPS/);
    await assert.rejects(prepare({ ...f.opts, mirrorUrl: 'https://user:password@example.invalid/file' }), /HTTPS/);
    assert.deepEqual(partials(f), []);
  } finally { f.close(); }
});
test('invalid manifest paths and symlink resources are rejected', async () => {
  const f = fixture();
  try {
    const malformed = structuredClone(f.manifest); malformed.asset.name = '../escape.zip';
    assert.throws(() => verify({ ...f.opts, manifest: malformed }), /manifest/);
    fs.writeFileSync(path.join(f.dir, 'real'), f.body);
    try { fs.symlinkSync(path.join(f.dir, 'real'), f.archive); }
    catch (error) { if (['EPERM', 'EACCES'].includes(error.code)) return; throw error; }
    assert.throws(() => verify(f.opts), /regular/);
  } finally { f.close(); }
});
