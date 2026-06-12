/* ═══════════════════════════════════════
   SigurdOS Web Flasher — Flash Logic
   ═══════════════════════════════════════
   Fetches release data and firmware from local API (firmware vault),
   not from GitHub. Data is synced by sync_firmware.py cron job.
   ═══════════════════════════════════════ */

const API_BASE = '/api';
const RELEASES_CACHE_KEY = 'sigurdos-releases-cache';
const RELEASES_CACHE_TTL = 5 * 60 * 1000; // 5 min

let selectedChannel = null;   // 'stable' | 'beta' | 'debug'
let releaseData = null;       // { stable: {...}, beta: {...} }
let serialPort = null;
let esptoolPromise = null;
let flashing = false;
let eraseAll = true;

// ── DOM refs ─────────────────────────────
const connectBtn = document.getElementById('btn-connect');
const flashBtn = document.getElementById('btn-flash');
const stableCard = document.getElementById('channel-stable');
const betaCard = document.getElementById('channel-beta');
const debugCard = document.getElementById('channel-debug');
const stableVersion = document.getElementById('stable-version');
const betaVersion = document.getElementById('beta-version');
const consoleEl = document.getElementById('console');
const consoleInner = document.getElementById('console-log');
const progressWrap = document.getElementById('progress-wrap');
const progressFill = document.getElementById('progress-fill');
const progressLabel = document.getElementById('progress-label');
const progressPercent = document.getElementById('progress-percent');
const deviceInfo = document.getElementById('device-info');
const chipName = document.getElementById('chip-name');
const chipMac = document.getElementById('chip-mac');
const chipFlash = document.getElementById('chip-flash');
const connectStatus = document.getElementById('connect-status');
const flashStatus = document.getElementById('flash-status');
const stepConnect = document.getElementById('step-connect');
const stepChannel = document.getElementById('step-channel');
const stepFlash = document.getElementById('step-flash');
const eraseToggle = document.getElementById('erase-all');

// ── Helpers ──────────────────────────────
function log(msg, cls = '') {
  const t = new Date().toLocaleTimeString();
  const line = `<span class="${cls}">[${t}] ${msg}</span>\n`;
  consoleInner.innerHTML += line;
  consoleEl.scrollTop = consoleEl.scrollHeight;
  consoleEl.classList.add('console--visible');
}

function setProgress(pct, label) {
  progressFill.style.width = `${pct}%`;
  progressLabel.textContent = label || '';
  progressPercent.textContent = `${pct}%`;
}

function showProgress(show) {
  progressWrap.classList.toggle('progress-wrap--visible', show);
}

function setStepStatus(step, status, text) {
  const badge = step.querySelector('.step__status');
  badge.className = 'step__status';
  if (status) badge.classList.add(`step__status--${status}`);
  if (text) badge.textContent = text;
  step.classList.remove('step--active', 'step--done', 'step--error');
  if (status === 'busy') step.classList.add('step--active');
  if (status === 'success') step.classList.add('step--done');
  if (status === 'error') step.classList.add('step--error');
}

function enableBtn(btn, enabled) {
  btn.disabled = !enabled;
}

// ── Local API ─────────────────────────────
async function fetchReleases() {
  // Check local cache first (avoids unnecessary requests)
  try {
    const cached = localStorage.getItem(RELEASES_CACHE_KEY);
    if (cached) {
      const { data, timestamp } = JSON.parse(cached);
      if (Date.now() - timestamp < RELEASES_CACHE_TTL) {
        releaseData = data;
        log('Using cached release data.', 'dim');
        updateChannelLabels();
        return;
      }
    }
  } catch (_) { /* ignore cache errors */ }

  log('Fetching release data...');
  const resp = await fetch(`${API_BASE}/releases`, {
    headers: { Accept: 'application/json' }
  });
  if (!resp.ok) throw new Error(`API error: ${resp.status}`);
  const releases = await resp.json();
  if (!releases.length) throw new Error('No releases found');

  const stable = releases.find(r => !r.prerelease) || null;
  const beta = releases.find(r => r.prerelease) || null;

  releaseData = { stable, beta };

  log(`Found ${releases.length} releases. Stable: ${releaseData.stable?.tag_name || 'none'}, Beta: ${releaseData.beta?.tag_name || 'none'}`);

  // Cache for 5 minutes
  try {
    localStorage.setItem(RELEASES_CACHE_KEY, JSON.stringify({
      data: releaseData,
      timestamp: Date.now()
    }));
  } catch (_) { /* ignore storage errors */ }

  updateChannelLabels();
}

function updateChannelLabels() {
  if (!releaseData) return;
  if (releaseData.stable) {
    stableVersion.textContent = releaseData.stable.tag_name;
  } else {
    stableVersion.textContent = 'No stable release yet';
    stableCard.classList.add('channel-card--unavailable');
    stableCard.style.cursor = 'default';
    stableCard.style.opacity = '0.4';
    log('All releases are currently pre-release. Stable channel will be available once a non-prerelease release is published.', 'orange');
  }
  if (releaseData.beta) {
    betaVersion.textContent = releaseData.beta.tag_name;
  }
}

function getFirmwareUrl(channel, filename) {
  // Local firmware URL: /api/firmware/<dev|latest|debug>/<filename>
  // dev -> latest prerelease, latest -> latest stable (or dev if no stable)
  // debug -> debug build (firmware-debug.bin)
  const channelPath = channel === 'stable' ? 'latest'
                    : channel === 'debug' ? 'debug'
                    : 'dev';
  return `${API_BASE}/firmware/${channelPath}/${filename}`;
}

async function downloadBinary(url) {
  log(`Downloading firmware binary...`);
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`Download failed: ${resp.status}`);
  const buf = await resp.arrayBuffer();
  log(`Downloaded ${buf.byteLength.toLocaleString()} bytes.`, 'green');
  return buf;
}

async function withTimeout(promise, ms, msg) {
  const timer = new Promise((_, reject) => setTimeout(() => reject(new Error(msg || `Timed out after ${ms}ms`)), ms));
  return Promise.race([promise, timer]);
}

function binaryToBinaryString(buf) {
  const bytes = new Uint8Array(buf);
  let s = '';
  for (let i = 0; i < bytes.length; i++) {
    s += String.fromCharCode(bytes[i]);
  }
  return s;
}

// ── WebSerial ─────────────────────────────
async function connectSerial() {
  if (serialPort) {
    await disconnectSerial();
    return;
  }

  enableBtn(connectBtn, false);
  setStepStatus(stepConnect, 'busy', 'Connecting');
  log('Requesting serial port...');

  try {
    const filters = [
      { usbVendorId: 0x303a }, // Espressif
      { usbVendorId: 0x1a86 }, // CH340/CH341
      { usbVendorId: 0x10c4 }, // CP210x
    ];
    serialPort = await navigator.serial.requestPort({ filters });

    const info = serialPort.getInfo || (() => ({}));
    const portInfo = info.call ? info.call(serialPort) : {};
    const usbVid = portInfo.usbVendorId?.toString(16) || '?';
    const usbPid = portInfo.usbProductId?.toString(16) || '?';
    log(`Connected: USB VID=${usbVid} PID=${usbPid}`);
    if (usbVid === '303a') {
      log('Native USB-serial-JTAG detected. Enter download mode before flashing: hold BOOT, tap RESET, release BOOT.', 'orange');
    }

    setStepStatus(stepConnect, 'success', 'Connected');
    connectBtn.textContent = 'Disconnect';
    enableBtn(connectBtn, true);
    log('Serial port ready for flashing.', 'green');
  } catch (err) {
    setStepStatus(stepConnect, 'error', 'Failed');
    enableBtn(connectBtn, true);
    log(`Connection failed: ${err.message}`, 'red');
    serialPort = null;
  }
}

async function disconnectSerial() {
  try {
    if (serialPort) {
      await serialPort.close();
    }
  } catch (e) {
    // ignore
  }
  serialPort = null;
  setStepStatus(stepConnect, 'ready', 'Not connected');
  connectBtn.textContent = 'Connect T-Deck';
  enableBtn(connectBtn, true);
  log('Serial disconnected.', 'dim');
}

// ── ESPTool Flash ─────────────────────────
async function flashFirmware() {
  if (flashing) return;
  if (!serialPort) {
    log('Connect a T-Deck first!', 'red');
    return;
  }
  if (!selectedChannel) {
    log('Select a firmware channel first!', 'red');
    return;
  }

  flashing = true;
  enableBtn(flashBtn, false);
  setStepStatus(stepFlash, 'busy', 'Flashing');
  showProgress(true);
  setProgress(2, 'Loading flasher library');
  consoleEl.classList.add('console--visible');

  const channel = selectedChannel;

  // Build download URL from local firmware vault
  const firmwareUrl = getFirmwareUrl(channel, channel === 'debug' ? 'firmware-debug.bin' : 'firmware-merged.bin');

  try {
    setProgress(5, 'Downloading firmware');
    const firmwareBuf = await downloadBinary(firmwareUrl);
    const binaryString = binaryToBinaryString(firmwareBuf);

    setProgress(15, 'Loading esptool-js');
    log('Loading browser flasher library...');
    const { ESPLoader, Transport, HardReset } = await loadEspTool();
    log('esptool-js loaded.', 'green');

    setProgress(20, 'Connecting to bootloader');
    log('Connecting to ESP32-S3 bootloader...');
    log('Tip: if it hangs, hold BOOT, tap RESET, release BOOT, then click Flash again.', 'dim');

    // Pass the port to esptool-js (it opens and manages the connection natively)
    const transport = new Transport(serialPort);
    const loader = new ESPLoader({
      transport,
      baudrate: 115200,
      romBaudrate: 115200,
      terminal: createFlashTerminal(),
      debugLogging: false,
    });
    loader.hr = new HardReset(transport);

    const chipInfo = await withTimeout(
      loader.main(),
      12000,
      'Timed out connecting to ESP32-S3. Put the T-Deck in download mode: hold BOOT, tap RESET, release BOOT, then click Flash.'
    );
    log(`Chip detected: ${chipInfo || 'ESP32-S3'}`, 'green');

    // Show device info
    let chipDesc = chipInfo || 'ESP32-S3';
    let macAddr = '?';
    let flashSize = '?';
    try {
      macAddr = await loader.chip.readMac(loader);
      flashSize = await loader.flashSize();
    } catch (e) {}

    chipName.textContent = chipDesc;
    chipMac.textContent = macAddr;
    chipFlash.textContent = typeof flashSize === 'string' ? flashSize : `${flashSize}MB`;
    deviceInfo.classList.add('device-info--visible');

    const tagName = releaseData?.[channel]?.tag_name || channel;
    const modeLabel = eraseAll ? 'Full Erase' : 'Update Only';
    setProgress(30, `${modeLabel}: writing firmware`);
    log(`Flashing ${tagName} (${channel} channel, ${modeLabel})...`);

    await loader.writeFlash({
      fileArray: [{ data: binaryString, address: 0 }],
      flashSize: 'keep',
      flashMode: 'keep',
      flashFreq: 'keep',
      eraseAll: eraseAll,
      compress: true,
      reportProgress: (_idx, written, total) => {
        const pct = total > 0 ? Math.round(30 + (written / total) * 65) : 30;
        setProgress(Math.min(95, pct), `Writing firmware (${(written / 1024 / 1024).toFixed(1)}/${(total / 1024 / 1024).toFixed(1)} MB)`);
      }
    });

    setProgress(97, 'Resetting device');
    log('Flash complete. Resetting...', 'green');

    try {
      if (typeof loader.after === 'function') {
        await loader.after('hard_reset');
      }
    } catch (e) {}

    setProgress(100, 'Done!');
    setStepStatus(stepFlash, 'success', 'Flashed!');
    log(`✓ ${tagName} flashed successfully!`, 'green');

    if (channel === 'debug') {
      log('Debug firmware flashed! Starting serial monitor...', 'green');
      flashBtn.textContent = 'Monitor Active';
      // Keep serial port open and start monitoring
      startSerialMonitor();
    } else {
      log('Your T-Deck is rebooting. It should boot into SigurdOS momentarily.', 'green');
      flashBtn.textContent = 'Flash Complete ✓';
      serialPort = null;
    }
  } catch (err) {
    setProgress(0, 'Error');
    setStepStatus(stepFlash, 'error', 'Failed');
    log(`Flash error: ${err.message}`, 'red');
    log('Try putting the T-Deck into bootloader mode manually: hold BOOT, tap RESET, release BOOT.', 'orange');
    flashing = false;
    enableBtn(flashBtn, true);
    flashBtn.textContent = 'Try Again';
  }

  flashing = false;
}

// ── ESPTool load + helpers ────────────────
async function loadEspTool() {
  if (!esptoolPromise) {
    esptoolPromise = import('/assets/vendor/esptool-js-bundle.js');
  }
  return esptoolPromise;
}

function createFlashTerminal() {
  return {
    clean() {},
    clear() {},
    write(v) { if (v != null) log(String(v).trim(), 'dim'); },
    writeLine(v) { if (v != null) log(String(v).trim(), 'dim'); },
    writeln(v) { if (v != null) log(String(v).trim(), 'dim'); },
    writeError(v) { if (v != null) log(String(v).trim(), 'red'); }
  };
}

// ── Serial Monitor (debug channel) ─────────
let captureRunning = false;
let captureBuffer = '';
let captureStartTime = 0;
let captureTimerInterval = null;

function showCaptureUI(show) {
  const el = document.getElementById('step-capture');
  if (show) {
    el.style.display = 'block';
    el.scrollIntoView({ behavior: 'smooth' });
    // Hide flash step status
    setStepStatus(stepFlash, 'success', 'Flashed!');
  } else {
    el.style.display = 'none';
  }
}

function logCapture(text, cls = '') {
  const el = document.getElementById('capture-log');
  const line = `<span class="${cls}">${escapeHtml(text)}</span>\n`;
  el.innerHTML += line;
  const console = document.getElementById('capture-console');
  console.scrollTop = console.scrollHeight;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function updateCaptureStats() {
  const elapsed = Math.floor((Date.now() - captureStartTime) / 1000);
  const mins = Math.floor(elapsed / 60);
  const secs = elapsed % 60;
  document.getElementById('capture-timer').textContent = `${mins}:${secs.toString().padStart(2, '0')}`;
  const kb = (captureBuffer.length / 1024).toFixed(1);
  document.getElementById('capture-bytes').textContent = `${kb} KB`;
}

async function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function startSerialMonitor() {
  if (!serialPort) {
    log('No serial port connected.', 'red');
    return;
  }

  captureRunning = true;
  captureBuffer = '';
  captureStartTime = Date.now();

  showCaptureUI(true);
  logCapture('Serial monitor started. Waiting for device to reboot...\n', 'dim');

  // Wait for device to reboot from flash
  await sleep(2000);

  // Flush any stale data
  try {
    if (serialPort.readable) {
      const flushReader = serialPort.readable.getReader();
      await sleep(500);
      flushReader.cancel();
    }
  } catch (_) {}

  logCapture('Listening...\n', 'green');

  // Start timer
  captureTimerInterval = setInterval(updateCaptureStats, 1000);

  try {
    while (captureRunning && serialPort.readable) {
      const reader = serialPort.readable.getReader();
      try {
        while (captureRunning) {
          const { value, done } = await reader.read();
          if (done) break;
          const text = new TextDecoder().decode(value);
          captureBuffer += text;
          logCapture(text);
        }
      } finally {
        reader.releaseLock();
      }
    }
  } catch (err) {
    if (captureRunning) {
      logCapture(`\n[error] ${err.message}`, 'red');
    }
  }

  logCapture('\nSerial monitor stopped.', 'orange');
  if (captureTimerInterval) {
    clearInterval(captureTimerInterval);
    captureTimerInterval = null;
  }
}

function stopCapture() {
  captureRunning = false;
  if (captureTimerInterval) {
    clearInterval(captureTimerInterval);
    captureTimerInterval = null;
  }
  setStepStatus(
    document.getElementById('step-capture'),
    'ready',
    'Stopped'
  );
  document.getElementById('capture-status').innerHTML = 'Stopped';

  // Disconnect serial port
  disconnectSerial();
}

function clearCapture() {
  document.getElementById('capture-log').innerHTML = '';
  captureBuffer = '';
  captureStartTime = Date.now();
}

function generateDebugReport(buffer, description, deviceInfo, duration) {
  const lines = [
    '╔══════════════════════════════════════════════╗',
    '║        SIGURDOS DEBUG REPORT                  ║',
    '╚══════════════════════════════════════════════╝',
    '',
    `Generated:    ${new Date().toISOString()}`,
    `Firmware:     SigurdOS-TDeck (debug build)`,
    `Captured:     ${duration}`,
    `Chip:         ${deviceInfo.chip || 'ESP32-S3'}`,
    `Flash:        ${deviceInfo.flash || '?'}`,
    '',
    '═══ USER DESCRIPTION ═══',
    description || '(none provided)',
    '',
    '═══ SERIAL OUTPUT ═══',
    buffer,
    '',
    '═══ END OF REPORT ═══',
    '',
    'Privacy: This report contains device diagnostics only.',
    'No GPS coordinates, message content, or personal data.',
    'Review before sharing.',
    ''
  ];
  return lines.join('\n');
}

function downloadDebugLog() {
  const description = document.getElementById('capture-description-input').value || '';
  const elapsed = Math.floor((Date.now() - captureStartTime) / 1000);
  const mins = Math.floor(elapsed / 60);
  const secs = elapsed % 60;
  const duration = `${mins}:${secs.toString().padStart(2, '0')}`;

  const deviceInfo = {
    chip: document.getElementById('chip-name')?.textContent || 'ESP32-S3',
    flash: document.getElementById('chip-flash')?.textContent || '?'
  };

  const report = generateDebugReport(captureBuffer, description, deviceInfo, duration);
  const blob = new Blob([report], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `sigurdos-debug-${new Date().toISOString().slice(0, 10)}.txt`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  logCapture('\n📄 Debug report downloaded.', 'green');
}

// ── Channel selection ─────────────────────
function selectChannel(channel) {
  // Don't allow selecting stable if no stable release
  if (channel === 'stable' && !releaseData?.stable) {
    log('No stable release available yet. Select Beta instead.', 'orange');
    return;
  }
  selectedChannel = channel;
  stableCard.classList.toggle('channel-card--selected', channel === 'stable');
  betaCard.classList.toggle('channel-card--selected', channel === 'beta');
  debugCard.classList.toggle('channel-card--selected', channel === 'debug');
  setStepStatus(stepChannel, 'success', 'Selected');
  enableBtn(flashBtn, !!(serialPort && releaseData));
  const label = channel === 'stable' ? 'Stable'
              : channel === 'debug' ? 'Debug'
              : 'Beta';
  flashBtn.textContent = `Flash ${label} Firmware`;
  log(`Selected ${label} channel: ${releaseData?.[channel]?.tag_name || channel}`, 'green');
}

// ── Init ──────────────────────────────────
async function init() {
  // Check WebSerial support
  if (!('serial' in navigator)) {
    log('Web Serial API not available. Use Chrome/Edge with HTTPS.', 'red');
    document.querySelector('.notice-bar').style.display = 'flex';
    return;
  }

  // Fetch releases from local API
  try {
    await fetchReleases();
  } catch (err) {
    log(`Failed to fetch releases: ${err.message}`, 'red');
    log('The page will still work, but firmware versions may not display.', 'orange');
  }

  // Auto-select beta (or stable if available)
  if (releaseData) {
    selectChannel(releaseData.stable ? 'stable' : 'beta');
  }
}

// ── Event listeners ───────────────────────
connectBtn.addEventListener('click', connectSerial);

stableCard.addEventListener('click', () => { if (releaseData?.stable) selectChannel('stable'); });
betaCard.addEventListener('click', () => selectChannel('beta'));
debugCard.addEventListener('click', () => selectChannel('debug'));

flashBtn.addEventListener('click', flashFirmware);

// Capture controls
document.getElementById('btn-download-log')?.addEventListener('click', downloadDebugLog);
document.getElementById('btn-clear-capture')?.addEventListener('click', clearCapture);
document.getElementById('btn-stop-capture')?.addEventListener('click', stopCapture);

if (eraseToggle) {
  eraseToggle.addEventListener('change', () => {
    eraseAll = eraseToggle.checked;
    log(`Flash mode: ${eraseAll ? 'Full Erase (wipes settings)' : 'Update Only (preserves settings)'}`, 'dim');
  });
}

// Kick off
init();
