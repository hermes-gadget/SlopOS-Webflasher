# WebSerial ReadableStream Locked — Root Cause & Fix

## Root Cause

The problem is in the esptool-js bundle (`/assets/vendor/esptool-js-bundle.js`). The `disconnect()` method calls `await this.reader.cancel()` but:

1. **No `reader.releaseLock()` call** — The reader lock is NEVER explicitly released. `cursor.cancel()` alone should eventually unlock the stream when the cancel completes, but...
2. **No `finally` block** — If `reader.cancel()` hangs (which happens when the stream enters an errored state, e.g. after flash completes), `device.close()` is never reached and the port stays open with a locked stream.
3. **No timeout on cancel** — `cancel()` can hang indefinitely in certain WebSerial error states.

## The Fix (app.js)

The app.js currently tries to call `transport.close()`, but the bundle's class has no `close()` method — only `disconnect()`. Even `disconnect()` is broken. The fix:

**Before** trying to close anything, manually release the reader lock. `reader.releaseLock()` is synchronous and always succeeds — it immediately unlocks the stream. THEN call disconnect to properly close the port.

## The Fix (esptool-js bundle)

The bundle's `disconnect()` needs `releaseLock()` in a `finally` block with a timeout on `cancel()`.
