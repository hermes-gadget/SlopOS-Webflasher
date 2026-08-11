import assert from 'node:assert/strict';
import test from 'node:test';

import { md5Hex } from '../assets/md5.js';

test('md5Hex matches RFC 1321 vectors', () => {
  const vectors = new Map([
    ['', 'd41d8cd98f00b204e9800998ecf8427e'],
    ['a', '0cc175b9c0f1b6a831c399e269772661'],
    ['abc', '900150983cd24fb0d6963f7d28e17f72'],
    ['message digest', 'f96b697d7cb7938d525a2f31aaf161d0'],
    ['abcdefghijklmnopqrstuvwxyz', 'c3fcd3d76192e4007dfb496cca67e13b'],
  ]);

  for (const [input, expected] of vectors) {
    assert.equal(md5Hex(input), expected, input || '<empty>');
  }
});

test('md5Hex accepts the binary strings used by esptool-js', () => {
  assert.equal(md5Hex(String.fromCharCode(0, 1, 127, 128, 255)), 'feea43e9b76fc31c34bcec403dcc4bf8');
});
