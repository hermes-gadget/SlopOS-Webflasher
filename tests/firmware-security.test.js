import assert from 'node:assert/strict';
import { generateKeyPairSync, sign } from 'node:crypto';
import test from 'node:test';

import {
  assertTargetDevice,
  buildFlashPlan,
  confirmFullErase,
  sha256Hex,
  validateManifest,
  verifyDetachedEd25519,
} from '../assets/firmware-security.js';


function sampleManifest() {
  return {
    schema_version: 1,
    release: 'beta-test',
    board: 'LILYGO T-Deck',
    chip: 'ESP32-S3',
    flash_size: 16 * 1024 * 1024,
    partition_layout: {
      name: 'partitions_sigurdos_16MB.csv',
      table_offset: 0x8000,
      app_offset: 0x10000,
      partitions: [
        { label: 'otadata', type: 1, subtype: 0, offset: 0xe000, size: 0x2000 },
        { label: 'app0', type: 0, subtype: 0x10, offset: 0x10000, size: 0x20000 },
        { label: 'spiffs', type: 1, subtype: 0x82, offset: 0x30000, size: 0x10000 },
      ],
    },
    modes: {
      full: [{ file: 'firmware-merged.bin', offset: 0, size: 64, sha256: '0'.repeat(64), kind: 'merged' }],
      update: [{ file: 'firmware.bin', offset: 0x10000, size: 64, sha256: '0'.repeat(64), kind: 'application' }],
      debug: [{ file: 'firmware-debug.bin', offset: 0, size: 64, sha256: '0'.repeat(64), kind: 'merged' }],
    },
  };
}

function arrayBuffer(bytes) {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}

function validEsp32S3Image() {
  const bytes = new Uint8Array(48);
  const view = new DataView(bytes.buffer);
  bytes[0] = 0xe9;
  bytes[1] = 1;
  bytes[2] = 2;
  bytes[12] = 9;
  bytes[23] = 0;
  view.setUint32(24, 0x3fc80000, true);
  view.setUint32(28, 4, true);
  bytes.set([1, 2, 3, 4], 32);
  bytes[47] = 0xef ^ 1 ^ 2 ^ 3 ^ 4;
  return bytes;
}

test('Ed25519 verification rejects modified signed manifest bytes', async () => {
  const { privateKey, publicKey } = generateKeyPairSync('ed25519');
  const message = new TextEncoder().encode('{"release":"beta-test"}\n');
  const signature = sign(null, message, privateKey).toString('base64');
  const publicDer = publicKey.export({ type: 'spki', format: 'der' });
  const rawPublic = publicDer.subarray(publicDer.length - 32).toString('base64');
  assert.equal(await verifyDetachedEd25519(arrayBuffer(message), signature, rawPublic), true);
  message[2] ^= 1;
  assert.equal(await verifyDetachedEd25519(arrayBuffer(message), signature, rawPublic), false);
});

test('chip identity and destructive confirmation fail closed', () => {
  const manifest = validateManifest(sampleManifest());
  assert.doesNotThrow(() => assertTargetDevice('ESP32-S3', manifest));
  assert.throws(() => assertTargetDevice('ESP32', manifest), /Refusing to write/);
  assert.doesNotThrow(() => confirmFullErase(() => 'ERASE T-DECK'));
  assert.throws(() => confirmFullErase(() => 'yes'), /Full erase cancelled/);
});

test('flash address and application image come from the validated manifest', async () => {
  const manifest = sampleManifest();
  const image = validEsp32S3Image();
  manifest.modes.update[0].size = image.byteLength;
  manifest.modes.update[0].sha256 = await sha256Hex(arrayBuffer(image));
  const plan = await buildFlashPlan(manifest, 'update', new Map([
    ['firmware.bin', arrayBuffer(image)],
  ]));
  assert.equal(plan.length, 1);
  assert.equal(plan[0].address, 0x10000);
});

test('filename-based images with invalid ESP headers are rejected', async () => {
  const manifest = sampleManifest();
  const image = new Uint8Array(64);
  manifest.modes.update[0].sha256 = await sha256Hex(arrayBuffer(image));
  await assert.rejects(
    buildFlashPlan(manifest, 'update', new Map([['firmware.bin', arrayBuffer(image)]])),
    /invalid magic/,
  );
});
