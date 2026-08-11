/**
 * Close a WebSerial port only after its active reader and read loop are done.
 * Errors intentionally propagate so callers can keep the port reference and
 * offer a retry instead of claiming cleanup succeeded.
 */
export async function closeSerialPort({ reader, readPromise, port }) {
  if (reader) await reader.cancel();
  if (readPromise) await readPromise;
  if (port) await port.close();
}
