'use strict';
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const assert = require('assert');
const net = require('net');
const ROOT = process.env.WXBOT_TEST_ROOT || path.resolve(__dirname, '..');
const filename = path.join(ROOT, 'desktop/src/main/backend.js');
const mod = { exports: {} };
vm.runInNewContext(fs.readFileSync(filename, 'utf8'), {
  require: name => name === './paths' ? {} : require(name),
  module: mod, process, setTimeout, clearTimeout,
}, { filename });
(async () => {
  const {findFreePort, tryPort} = mod.exports;
  const server = net.createServer();
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  try {
    const busyPort = server.address().port;
    assert.strictEqual(await tryPort(busyPort),null);
    const fallback = (await tryPort(busyPort)) || (await findFreePort());
    assert.ok(fallback > 0 && fallback <= 65535, `invalid fallback port: ${fallback}`);
    assert.notStrictEqual(fallback,busyPort);
    const probe = net.createServer();
    await new Promise((resolve,reject) => {probe.on('error',reject);probe.listen(fallback,'127.0.0.1',resolve);});
    await new Promise(resolve => probe.close(resolve));
    console.log('PASS desktop port: occupied preferred port returns usable positive fallback');
  } finally {await new Promise(resolve => server.close(resolve));}
})().catch(e => {console.error(e);process.exitCode=1;});
