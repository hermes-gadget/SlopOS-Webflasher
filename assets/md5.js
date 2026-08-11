// RFC 1321 MD5 for esptool-js flash readback verification.
// This is an integrity check only; signed SHA-256 manifests remain the
// authenticity boundary for firmware.

const SHIFT_AMOUNTS = [
  7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
  5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
  4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
  6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
];

const ROUND_CONSTANTS = Array.from(
  { length: 64 },
  (_, index) => Math.floor(Math.abs(Math.sin(index + 1)) * 0x100000000) >>> 0,
);

function toBytes(input) {
  if (typeof input === 'string') {
    const bytes = new Uint8Array(input.length);
    for (let index = 0; index < input.length; index += 1) {
      const value = input.charCodeAt(index);
      if (value > 0xff) throw new TypeError('MD5 input must be a binary string');
      bytes[index] = value;
    }
    return bytes;
  }
  if (input instanceof ArrayBuffer) return new Uint8Array(input);
  if (ArrayBuffer.isView(input)) {
    return new Uint8Array(input.buffer, input.byteOffset, input.byteLength);
  }
  throw new TypeError('MD5 input must be a binary string or byte array');
}

function leftRotate(value, amount) {
  return ((value << amount) | (value >>> (32 - amount))) >>> 0;
}

function wordToHex(word) {
  return [0, 8, 16, 24]
    .map(shift => ((word >>> shift) & 0xff).toString(16).padStart(2, '0'))
    .join('');
}

/** Return the lowercase MD5 digest of the binary string or byte array input. */
export function md5Hex(input) {
  const bytes = toBytes(input);
  const bitLength = bytes.length * 8;
  const paddedLength = ((bytes.length + 9 + 63) >> 6) << 6;
  const padded = new Uint8Array(paddedLength);
  padded.set(bytes);
  padded[bytes.length] = 0x80;

  const view = new DataView(padded.buffer);
  view.setUint32(paddedLength - 8, bitLength >>> 0, true);
  view.setUint32(paddedLength - 4, Math.floor(bitLength / 0x100000000) >>> 0, true);

  let a0 = 0x67452301;
  let b0 = 0xefcdab89;
  let c0 = 0x98badcfe;
  let d0 = 0x10325476;

  for (let offset = 0; offset < padded.length; offset += 64) {
    const words = new Uint32Array(16);
    for (let index = 0; index < 16; index += 1) {
      words[index] = view.getUint32(offset + index * 4, true);
    }

    let a = a0;
    let b = b0;
    let c = c0;
    let d = d0;

    for (let index = 0; index < 64; index += 1) {
      let f;
      let wordIndex;
      if (index < 16) {
        f = (b & c) | (~b & d);
        wordIndex = index;
      } else if (index < 32) {
        f = (d & b) | (~d & c);
        wordIndex = (5 * index + 1) % 16;
      } else if (index < 48) {
        f = b ^ c ^ d;
        wordIndex = (3 * index + 5) % 16;
      } else {
        f = c ^ (b | ~d);
        wordIndex = (7 * index) % 16;
      }
      const next = d;
      const sum = (a + f + ROUND_CONSTANTS[index] + words[wordIndex]) >>> 0;
      d = c;
      c = b;
      b = (b + leftRotate(sum, SHIFT_AMOUNTS[index])) >>> 0;
      a = next;
    }

    a0 = (a0 + a) >>> 0;
    b0 = (b0 + b) >>> 0;
    c0 = (c0 + c) >>> 0;
    d0 = (d0 + d) >>> 0;
  }

  return wordToHex(a0) + wordToHex(b0) + wordToHex(c0) + wordToHex(d0);
}
