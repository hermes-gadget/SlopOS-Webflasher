import assert from 'node:assert/strict';
import test from 'node:test';

import { closeSerialPort } from '../assets/serial-cleanup.js';

test('idle stream cancellation drains the reader before closing the port', async () => {
  const events = [];
  const reader = {
    async cancel() {
      events.push('cancel');
    },
  };
  const readPromise = Promise.resolve().then(() => events.push('read-drained'));
  const port = {
    async close() {
      events.push('close');
    },
  };

  await closeSerialPort({ reader, readPromise, port });
  assert.deepEqual(events, ['cancel', 'read-drained', 'close']);
});

test('continuous stream cancellation waits for the pending read to settle', async () => {
  const events = [];
  let finishRead;
  const readPromise = new Promise(resolve => {
    finishRead = resolve;
  });
  const reader = {
    async cancel() {
      events.push('cancel');
      finishRead();
    },
  };
  const port = {
    async close() {
      events.push('close');
    },
  };

  await closeSerialPort({ reader, readPromise, port });
  assert.deepEqual(events, ['cancel', 'close']);
});
