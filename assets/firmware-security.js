const MANIFEST_SCHEMA_VERSION = 1;
const TARGET_BOARD = 'LILYGO T-Deck';
const TARGET_CHIP = 'ESP32-S3';
const TDECK_FLASH_SIZE = 16 * 1024 * 1024;
const PARTITION_TABLE_OFFSET = 0x8000;
const PARTITION_TABLE_SIZE = 0x1000;
const BOOT_APP0_OFFSET = 0xe000;
const APP_OFFSET = 0x10000;
const MAX_MANIFEST_SIZE = 64 * 1024;
const ESP_IMAGE_MAGIC = 0xe9;
const ESP32S3_CHIP_ID = 9;
const MAX_IMAGE_SEGMENTS = 16;

// Raw Ed25519 public key. Its private half is kept outside this public repository.
const PINNED_MANIFEST_PUBLIC_KEY_BASE64 = 'eb6Xn3crT3paJHg2dJc3KUCn7ZCIl+wCEFoqf00OPlw=';

const EXPECTED_MODES = Object.freeze({
  full: Object.freeze({ file: 'firmware-merged.bin', offset: 0, kind: 'merged' }),
  update: Object.freeze({ file: 'firmware.bin', offset: APP_OFFSET, kind: 'application' }),
  debug: Object.freeze({ file: 'firmware-debug.bin', offset: 0, kind: 'merged' }),
});

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function hasExactKeys(value, expected) {
  const keys = Object.keys(value).sort();
  return keys.length === expected.length && keys.every((key, index) => key === [...expected].sort()[index]);
}

function requireExactKeys(value, expected, label) {
  if (!isPlainObject(value) || !hasExactKeys(value, expected)) {
    throw new Error(`${label} has unsupported or missing fields`);
  }
}

function requireInteger(value, label) {
  if (!Number.isSafeInteger(value)) throw new Error(`${label} must be an integer`);
  return value;
}

function validateManifest(manifest, expectedRelease = '') {
  requireExactKeys(manifest, [
    'schema_version', 'release', 'board', 'chip', 'flash_size', 'partition_layout', 'modes'
  ], 'Manifest');
  if (manifest.schema_version !== MANIFEST_SCHEMA_VERSION) throw new Error('Unsupported firmware manifest schema');
  if (typeof manifest.release !== 'string' || !manifest.release || manifest.release.length > 128) {
    throw new Error('Manifest release must be a non-empty bounded string');
  }
  if (expectedRelease && manifest.release !== expectedRelease) {
    throw new Error('Signed manifest release does not match the selected release');
  }
  if (manifest.board !== TARGET_BOARD || manifest.chip !== TARGET_CHIP) {
    throw new Error('Firmware manifest is not for the LILYGO T-Deck / ESP32-S3');
  }
  if (manifest.flash_size !== TDECK_FLASH_SIZE) throw new Error('Firmware manifest has the wrong T-Deck flash size');

  const layout = manifest.partition_layout;
  requireExactKeys(layout, ['name', 'table_offset', 'app_offset', 'partitions'], 'Partition layout');
  if (layout.name !== 'partitions_sigurdos_16MB.csv' ||
      layout.table_offset !== PARTITION_TABLE_OFFSET || layout.app_offset !== APP_OFFSET) {
    throw new Error('Firmware manifest has an unexpected T-Deck partition layout');
  }
  if (!Array.isArray(layout.partitions) || layout.partitions.length === 0) {
    throw new Error('Firmware manifest has no partition layout');
  }
  const labels = new Set();
  let previousEnd = 0;
  let appCount = 0;
  for (const [index, part] of layout.partitions.entries()) {
    requireExactKeys(part, ['label', 'type', 'subtype', 'offset', 'size'], `Partition ${index}`);
    if (typeof part.label !== 'string' || !part.label || part.label.length > 16 || !/^[\x20-\x7e]+$/.test(part.label)) {
      throw new Error(`Partition ${index} has an invalid label`);
    }
    if (labels.has(part.label)) throw new Error(`Partition ${part.label} is repeated`);
    labels.add(part.label);
    requireInteger(part.type, `Partition ${part.label} type`);
    requireInteger(part.subtype, `Partition ${part.label} subtype`);
    requireInteger(part.offset, `Partition ${part.label} offset`);
    requireInteger(part.size, `Partition ${part.label} size`);
    if (part.type < 0 || part.type > 0xff || part.subtype < 0 || part.subtype > 0xff ||
        part.offset < previousEnd || part.size <= 0 || part.offset + part.size > TDECK_FLASH_SIZE) {
      throw new Error(`Partition ${part.label} has unsafe or overlapping bounds`);
    }
    previousEnd = part.offset + part.size;
    if (part.type === 0 && part.offset === APP_OFFSET) appCount += 1;
  }
  if (appCount !== 1) throw new Error('Partition layout must contain one application at 0x10000');

  requireExactKeys(manifest.modes, Object.keys(EXPECTED_MODES), 'Manifest modes');
  const files = new Set();
  for (const [mode, expected] of Object.entries(EXPECTED_MODES)) {
    const images = manifest.modes[mode];
    if (!Array.isArray(images) || images.length !== 1) throw new Error(`${mode} mode must contain exactly one image`);
    const image = images[0];
    requireExactKeys(image, ['file', 'offset', 'size', 'sha256', 'kind'], `${mode} image`);
    if (image.file !== expected.file || image.offset !== expected.offset || image.kind !== expected.kind) {
      throw new Error(`${mode} mode does not match the signed T-Deck flash contract`);
    }
    requireInteger(image.offset, `${mode} image offset`);
    requireInteger(image.size, `${mode} image size`);
    if (image.size <= 0 || image.offset < 0 || image.offset + image.size > TDECK_FLASH_SIZE) {
      throw new Error(`${mode} image exceeds T-Deck flash bounds`);
    }
    if (typeof image.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(image.sha256)) {
      throw new Error(`${mode} image has an invalid SHA-256 digest`);
    }
    if (files.has(image.file)) throw new Error(`Firmware file ${image.file} is repeated`);
    files.add(image.file);
  }
  return manifest;
}

function base64Bytes(text) {
  if (typeof text !== 'string' || !/^[A-Za-z0-9+/]+={0,2}$/.test(text)) {
    throw new Error('Firmware manifest signature is not valid base64');
  }
  const binary = atob(text);
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

async function verifyDetachedEd25519(manifestBuffer, signatureText, publicKeyBase64) {
  const bytes = new Uint8Array(manifestBuffer);
  if (bytes.byteLength === 0 || bytes.byteLength > MAX_MANIFEST_SIZE) {
    throw new Error('Firmware manifest is empty or too large');
  }
  const signature = base64Bytes(signatureText.trim());
  if (signature.byteLength !== 64) throw new Error('Firmware manifest signature has the wrong length');
  const publicKey = base64Bytes(publicKeyBase64);
  const key = await crypto.subtle.importKey('raw', publicKey, { name: 'Ed25519' }, false, ['verify']);
  return crypto.subtle.verify({ name: 'Ed25519' }, key, signature, bytes);
}

async function verifySignedManifest(manifestBuffer, signatureText, expectedRelease = '') {
  const bytes = new Uint8Array(manifestBuffer);
  const verified = await verifyDetachedEd25519(
    manifestBuffer,
    signatureText,
    PINNED_MANIFEST_PUBLIC_KEY_BASE64,
  );
  if (!verified) throw new Error('Firmware manifest signature does not match the pinned release key');
  let manifest;
  try {
    manifest = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
  } catch (_) {
    throw new Error('Firmware manifest is not valid UTF-8 JSON');
  }
  return validateManifest(manifest, expectedRelease);
}

function assertTargetDevice(detectedChip, manifest) {
  if (detectedChip !== TARGET_CHIP || manifest.chip !== TARGET_CHIP || manifest.board !== TARGET_BOARD) {
    throw new Error(`Refusing to write: detected ${detectedChip || 'unknown'}, but signed firmware requires ${TARGET_BOARD} (${TARGET_CHIP})`);
  }
}

function confirmFullErase(promptFunction) {
  const phrase = 'ERASE T-DECK';
  const response = promptFunction(
    `DANGER: Full Erase permanently wipes every setting on the connected ${TARGET_CHIP}. ` +
    `Confirm that this device is a ${TARGET_BOARD} by typing ${phrase}.`
  );
  if (response !== phrase) throw new Error('Full erase cancelled: destructive T-Deck confirmation did not match');
}

function readUint32(bytes, offset) {
  return new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(offset, true);
}

function alignUp(value, alignment) {
  return Math.ceil(value / alignment) * alignment;
}

async function validateEsp32S3Image(bytes, offset) {
  const headerSize = 24;
  if (offset < 0 || offset + headerSize > bytes.byteLength) throw new Error(`ESP image header at 0x${offset.toString(16)} is truncated`);
  const segmentCount = bytes[offset + 1];
  const chipId = bytes[offset + 12] | (bytes[offset + 13] << 8);
  const appendDigest = bytes[offset + 23];
  if (bytes[offset] !== ESP_IMAGE_MAGIC) throw new Error(`ESP image at 0x${offset.toString(16)} has invalid magic`);
  if (segmentCount < 1 || segmentCount > MAX_IMAGE_SEGMENTS) throw new Error('ESP image has an invalid segment count');
  if (chipId !== ESP32S3_CHIP_ID) throw new Error('ESP image header does not target ESP32-S3');
  if (appendDigest !== 0 && appendDigest !== 1) throw new Error('ESP image has an invalid append-digest flag');

  let position = offset + headerSize;
  let checksum = 0xef;
  for (let index = 0; index < segmentCount; index += 1) {
    if (position + 8 > bytes.byteLength) throw new Error(`ESP image segment ${index} header is truncated`);
    const segmentSize = readUint32(bytes, position + 4);
    if (segmentSize === 0 || segmentSize % 4 !== 0) throw new Error(`ESP image segment ${index} has an invalid size`);
    position += 8;
    if (position + segmentSize > bytes.byteLength) throw new Error(`ESP image segment ${index} data is truncated`);
    for (let cursor = position; cursor < position + segmentSize; cursor += 1) checksum ^= bytes[cursor];
    position += segmentSize;
  }
  const checksumEnd = alignUp(position + 1, 16);
  if (checksumEnd > bytes.byteLength || bytes[checksumEnd - 1] !== checksum) {
    throw new Error('ESP image checksum is missing or invalid');
  }
  const imageEnd = checksumEnd + (appendDigest ? 32 : 0);
  if (imageEnd > bytes.byteLength) throw new Error('ESP image digest is truncated');
  if (appendDigest) {
    const expected = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes.slice(offset, checksumEnd)));
    const actual = bytes.slice(checksumEnd, imageEnd);
    if (!expected.every((value, index) => value === actual[index])) throw new Error('ESP image embedded SHA-256 digest is invalid');
  }
}

function decodePartitionLabel(bytes) {
  const end = bytes.indexOf(0);
  const labelBytes = end === -1 ? bytes : bytes.slice(0, end);
  if (labelBytes.byteLength === 0 || labelBytes.some((value) => value < 0x20 || value > 0x7e)) {
    throw new Error('Partition table has an invalid label');
  }
  return String.fromCharCode(...labelBytes);
}

function parsePartitionTable(bytes, tableOffset) {
  if (tableOffset + PARTITION_TABLE_SIZE > bytes.byteLength) throw new Error('Merged image is truncated before the partition table');
  const entries = [];
  let foundMd5 = false;
  for (let position = tableOffset; position + 32 <= tableOffset + PARTITION_TABLE_SIZE; position += 32) {
    const magic = bytes[position] | (bytes[position + 1] << 8);
    if (magic === 0xffff) break;
    if (magic === 0xebeb) {
      foundMd5 = true;
      break;
    }
    if (magic !== 0x50aa) throw new Error(`Partition table has invalid magic at 0x${position.toString(16)}`);
    const part = {
      label: decodePartitionLabel(bytes.slice(position + 12, position + 28)),
      type: bytes[position + 2],
      subtype: bytes[position + 3],
      offset: readUint32(bytes, position + 4),
      size: readUint32(bytes, position + 8),
    };
    if (part.size <= 0 || part.offset + part.size > TDECK_FLASH_SIZE) throw new Error('Partition table contains an invalid range');
    entries.push(part);
  }
  if (entries.length === 0 || !foundMd5) throw new Error('Partition table has no entries or ESP-IDF MD5 record');
  entries.sort((left, right) => left.offset - right.offset);
  for (let index = 1; index < entries.length; index += 1) {
    if (entries[index].offset < entries[index - 1].offset + entries[index - 1].size) {
      throw new Error('Partition table contains overlapping ranges');
    }
  }
  return entries;
}

async function sha256Hex(buffer) {
  const digest = await crypto.subtle.digest('SHA-256', buffer);
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('');
}

async function validateImageBuffer(buffer, image, manifest) {
  if (!(buffer instanceof ArrayBuffer)) throw new Error(`${image.file} did not download as binary data`);
  if (buffer.byteLength !== image.size) throw new Error(`${image.file} size differs from the signed manifest`);
  if (await sha256Hex(buffer) !== image.sha256) throw new Error(`${image.file} digest differs from the signed manifest`);
  const bytes = new Uint8Array(buffer);
  if (image.kind === 'application') {
    await validateEsp32S3Image(bytes, 0);
    return;
  }
  await validateEsp32S3Image(bytes, 0);
  const parsed = parsePartitionTable(bytes, manifest.partition_layout.table_offset);
  if (JSON.stringify(parsed) !== JSON.stringify(manifest.partition_layout.partitions)) {
    throw new Error(`${image.file} partition table differs from the signed layout`);
  }
  if (bytes.byteLength < APP_OFFSET || readUint32(bytes, BOOT_APP0_OFFSET) !== 1) {
    throw new Error(`${image.file} does not contain the required boot_app0 component`);
  }
  await validateEsp32S3Image(bytes, manifest.partition_layout.app_offset);
}

function binaryToBinaryString(buffer) {
  const bytes = new Uint8Array(buffer);
  const chunks = [];
  const chunkSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    chunks.push(String.fromCharCode(...bytes.subarray(offset, offset + chunkSize)));
  }
  return chunks.join('');
}

async function buildFlashPlan(manifest, mode, binaries) {
  validateManifest(manifest);
  if (!Object.hasOwn(EXPECTED_MODES, mode)) throw new Error('Unknown firmware flash mode');
  const plan = [];
  for (const image of manifest.modes[mode]) {
    const buffer = binaries.get(image.file);
    if (!buffer) throw new Error(`Missing downloaded firmware image ${image.file}`);
    await validateImageBuffer(buffer, image, manifest);
    plan.push({ data: binaryToBinaryString(buffer), address: image.offset });
  }
  for (let index = 1; index < plan.length; index += 1) {
    const previousImage = manifest.modes[mode][index - 1];
    if (plan[index].address < plan[index - 1].address + previousImage.size) {
      throw new Error('Signed firmware images overlap in flash');
    }
  }
  return plan;
}

export {
  PINNED_MANIFEST_PUBLIC_KEY_BASE64,
  TARGET_BOARD,
  TARGET_CHIP,
  assertTargetDevice,
  buildFlashPlan,
  confirmFullErase,
  sha256Hex,
  validateImageBuffer,
  validateManifest,
  verifyDetachedEd25519,
  verifySignedManifest,
};
