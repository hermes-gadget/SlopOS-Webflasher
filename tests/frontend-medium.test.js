import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const app = readFileSync(new URL('../assets/app.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../assets/styles.css', import.meta.url), 'utf8');

test('update mode uses the manifest filename for both download and digest validation', () => {
  assert.match(app, /downloadBinary\(getFirmwareUrl\(channel, image\.file\), image\)/);
  assert.match(app, /buildFlashPlan\(manifest, mode, binaries\)/);
  assert.doesNotMatch(app, /api\/hashes/);
});

test('flash readback verification is required before success/reset', () => {
  assert.match(app, /calculateMD5Hash: md5Hex/);
  assert.match(app, /flashMd5sum result before writeFlash resolves/);
  assert.match(app, /Flash readback verification failed/);
});

test('console and monitor output never interpolate untrusted text as HTML', () => {
  assert.match(app, /line\.textContent = String\(text\)/);
  assert.doesNotMatch(app, /consoleInner\.innerHTML/);
  assert.doesNotMatch(app, /captureStatus\.innerHTML/);
  assert.doesNotMatch(app, /capture-log'\)\.innerHTML/);
  assert.match(app, /Privacy warning: Serial output may contain sensitive data/);
  assert.doesNotMatch(app, /No GPS coordinates, message content, or personal data\./);
  assert.match(html, /id="review-debug-log"/);
  assert.match(html, /Serial logs may contain sensitive data/);
});

test('stopping the monitor cancels and drains the reader before closing the port', () => {
  assert.match(app, /import \{ closeSerialPort \} from '\/assets\/serial-cleanup\.js'/);
  assert.match(app, /await closeSerialPort\(\{\n\s+reader: activeCaptureReader,/);
  assert.match(app, /readPromise: captureReadPromise/);
  assert.match(app, /reader\.releaseLock\(\)/);
  assert.match(app, /Keep serialPort set so the caller can retry/);
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  assert.equal(new Set(ids).size, ids.length, 'HTML IDs must be unique');
  const queriedIds = new Set([...app.matchAll(/getElementById\(['"]([^'"]+)['"]\)/g)].map(match => match[1]));
  for (const id of queriedIds) assert.ok(ids.includes(id), `missing DOM id: ${id}`);
  assert.match(app, /resetConstructors: \{[\s\S]*usbJTAGSerialReset/);
  assert.doesNotMatch(app, /loader\.hr/);
  assert.match(styles, /\.step--active \.step__status/);
  assert.doesNotMatch(styles, /\.step--busy/);
});
