/* ═══════════════════════════════════════
   SlopOS Web Flasher — Flash Logic
   ═══════════════════════════════════════ */

const GITHUB_OWNER = 'hermes-gadget';
const GITHUB_REPO = 'SlopOS-tdeck';
const API_BASE = `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}`;
const RELEASES_CACHE_KEY = 'slopos-releases-cache';
const RELEASES_CACHE_TTL = 5 * 60 * 1000; // 5 min

let selectedChannel = null;   // 'stable' | 'beta'
let releaseData = null;       // { stable: {...}, beta: {...} }
let serialPort = null;
let esptoolPromise = null;
let flashing = false;

// ── DOM refs ─────────────────────────────
const connectBtn = document.getElementById('btn-connect');
const flashBtn = document.getElementById('btn-flash');
const stableCard = document.getElementById('channel-stable');
const betaCard = document.getElementById('channel-beta');
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

// ── GitHub API ────────────────────────────
async function fetchReleases() {
  log('Fetching release data from GitHub...');
  const resp = await fetch(`${API_BASE}/releases?per_page=10`, {
    headers: { Accept: 'application/vnd.github.v3+json' }
  });
  if (!resp.ok) throw new Error(`GitHub API error: ${resp.status}`);
  const releases = await resp.json();
  if (!releases.length) throw new Error('No releases found');

  // Classify: stable = first non-prerelease, beta = latest prerelease
  const stable = releases.find(r => !r.prerelease);
  const beta = releases.find(r => r.prerelease);

  const result = { stable, beta };
  if (result.stable) result.stable.tag_display = result.stable.tag_name;
  if (result.beta) result.beta.tag_display = result.beta.tag_name;

  releaseData = result;
  log(`Found ${releases.length} releases. Stable: ${result.stable?.tag_name || 'none'}, Beta: ${result.beta?.tag_name || 'none'}`);

  // Update version labels
  if (result.stable) {
    stableVersion.textContent = result.stable.tag_name;
  } else {
    stableVersion.textContent = 'No stable release yet';
    stableCard.classList.add('channel-card--unavailable');
    stableCard.style.cursor = 'default';
    stableCard.style.opacity = '0.4';
    log('All releases are currently pre-release. Stable channel will be available once a non-prerelease release is published.', 'orange');
  }
  if (result.beta) {
    betaVersion.textContent = result.beta.tag_name;
  }
}

function getReleaseAsset(release, name) {
  if (!release || !release.assets) return null;
  return release.assets.find(a => a.name === name);
}

async function downloadBinary(url) {
  log(`Downloading firmware binary...`);
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`Download failed: ${resp.status}`);
  const buf = await resp.arrayBuffer();
  log(`Downloaded ${buf.byteLength.toLocaleString()} bytes.`, 'green');
  return buf;
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
    log(`Connected: USB VID=${portInfo.usbVendorId?.toString(16) || '?'} PID=${portInfo.usbProductId?.toString(16) || '?'}`);

    // Open the port for later flashing (esptool-js handles this)
    await serialPort.open({ baudRate: 115200 });

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

  const release = releaseData[selectedChannel];
  if (!release) {
    log(`No ${selectedChannel} release available.`, 'red');
    setStepStatus(stepFlash, 'error', 'Failed');
    flashing = false;
    enableBtn(flashBtn, true);
    return;
  }

  const asset = getReleaseAsset(release, 'firmware-merged.bin');
  if (!asset) {
    log(`No firmware-merged.bin found in ${release.tag_name}.`, 'red');
    setStepStatus(stepFlash, 'error', 'Failed');
    flashing = false;
    enableBtn(flashBtn, true);
    return;
  }

  try {
    setProgress(5, 'Downloading firmware');
    const firmwareBuf = await downloadBinary(asset.browser_download_url);
    const binaryString = binaryToBinaryString(firmwareBuf);

    setProgress(15, 'Loading esptool-js');
    log('Loading browser flasher library...');
    const { ESPLoader, Transport, HardReset } = await loadEspTool();
    log('esptool-js loaded.', 'green');

    setProgress(20, 'Connecting to bootloader');
    log('Connecting to ESP32-S3 bootloader...');

    // Close the serial port first (esptool-js will reopen)
    try { await serialPort.close(); } catch (e) {}

    const transport = new Transport(serialPort, true);
    const loader = new ESPLoader({
      transport,
      baudrate: 115200,
      romBaudrate: 115200,
      terminal: createFlashTerminal(),
      debugLogging: false,
    });
    loader.hr = new HardReset(transport);

    const chipInfo = await loader.main();
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

    setProgress(30, 'Erasing and writing firmware');
    log(`Flashing ${release.tag_name} (${selectedChannel} channel)...`);

    await loader.writeFlash({
      fileArray: [{ data: binaryString, address: 0 }],
      flashSize: 'keep',
      flashMode: 'keep',
      flashFreq: 'keep',
      eraseAll: true,
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
    log(`✓ ${release.tag_name} flashed successfully!`, 'green');
    log('Your T-Deck is rebooting. It should boot into SlopOS momentarily.', 'green');
    flashBtn.textContent = 'Flash Complete ✓';
    serialPort = null;
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
  setStepStatus(stepChannel, 'success', 'Selected');
  enableBtn(flashBtn, !!(serialPort && releaseData));
  const label = channel === 'stable' ? 'Stable' : 'Beta';
  flashBtn.textContent = `Flash ${label} Firmware`;
  log(`Selected ${label} channel: ${releaseData[channel]?.tag_name || 'latest'}`, 'green');
}

// ── Init ──────────────────────────────────
async function init() {
  // Check WebSerial support
  if (!('serial' in navigator)) {
    log('Web Serial API not available. Use Chrome/Edge with HTTPS.', 'red');
    document.querySelector('.notice-bar').style.display = 'flex';
    return;
  }

  // Fetch releases
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

flashBtn.addEventListener('click', flashFirmware);

// Kick off
init();
