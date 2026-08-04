# SigurdOS Web Flasher

Browser-based firmware flasher for the LilyGo T-Deck running [SigurdOS](https://github.com/hermes-gadget/SigurdOS-tdeck).

Flash firmware directly from the browser over WebSerial. No tools, no accounts, no downloads required.

## Features

- **Zero-install** — open the page, connect your T-Deck, and flash
- **Stable & Beta channels** — pick your firmware version from GitHub releases
- **Integrity-verified** — SHA-256 hashes are checked in-browser before every flash
- **Pixel theme** — matches the sigurdos.dev design language
- **Single merged binary** — downloads `firmware-merged.bin` and flashes at offset 0x0
- **Small Python backend** — serves the UI and a local firmware vault with path-traversal protection and strict security headers

## Quick Start

### Run locally

```bash
python3 server.py          # serves UI + firmware vault on http://127.0.0.1:8082
```

The firmware vault (`~/firmware/vault`) is populated by `sync_firmware.py`, which mirrors release binaries from GitHub.

### Run with Docker

```bash
docker compose up --build -d
```

Open `http://127.0.0.1:8081`.

## Architecture

The flasher is a small Python backend (`server.py`) serving a static single-page frontend:

- `server.py` — HTTP server: static UI, firmware vault, `/api/releases` + `/api/hashes` endpoints, path-traversal protection, strict security headers
- `sync_firmware.py` — mirrors firmware release binaries from GitHub into the local vault
- `nginx.conf` — production reverse proxy (Docker)
- `index.html` — pixel-themed UI (Press Start 2P headers, Pixelify Sans body)
- `assets/app.js` — channel selection, SHA-256 verification, esptool-js flashing
- `assets/styles.css` — pixel theme matching sigurdos.dev
- `assets/vendor/esptool-js-bundle.js` — browser ESP32 flashing library

Firmware binaries are synced from GitHub release assets into a local vault — they are not bundled in this repo.

## How it works

1. **Connect** — browser WebSerial connects to the T-Deck's UART
2. **Choose channel** — pick Stable (latest release) or Beta (latest pre-release)
3. **Fetch** — downloads `firmware-merged.bin` from the local server (`/latest/` or `/dev/`)
4. **Verify** — browser computes the file's SHA-256 and compares it against the hash published by the server (`/api/hashes`)
5. **Flash** — esptool-js writes the verified binary via serial bootloader
6. **Reset** — device reboots into SigurdOS

## Requirements

- Chrome, Edge, or Opera (WebSerial API)
- HTTPS or localhost
- LilyGo T-Deck (ESP32-S3) with USB connected
- Firmware release on GitHub with a `firmware-merged.bin` asset

### Manual bootloader entry

If automatic bootloader detection fails:

1. Hold the **BOOT** button on the T-Deck
2. Tap **RESET**
3. Release **BOOT**
4. Click **Flash** in the browser (within 5 seconds)

## Development

```bash
git clone https://github.com/hermes-gadget/SigurdOS-Webflasher.git
cd SigurdOS-Webflasher
python3 server.py
```

Open `http://127.0.0.1:8082`.
