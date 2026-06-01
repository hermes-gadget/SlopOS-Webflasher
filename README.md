# SigurdOS Web Flasher

Browser-based firmware flasher for the LilyGo T-Deck running [SigurdOS](https://github.com/hermes-gadget/SigurdOS-tdeck).

Flash firmware directly from the browser over WebSerial. No tools, no accounts, no downloads required.

## Features

- **Zero-install** — open the page, connect your T-Deck, and flash
- **Stable & Beta channels** — pick your firmware version from GitHub releases
- **Pixel theme** — matches the sigurdos.dev design language
- **Single merged binary** — downloads `firmware-merged.bin` from GitHub and flashes at offset 0x0
- **No backend** — fully static, fetches binaries directly from GitHub

## Quick Start

### Run locally

```bash
python3 -m http.server 8080
```

Open `http://127.0.0.1:8080`.

### Run with Docker

```bash
docker compose up --build -d
```

Open `http://127.0.0.1:8081`.

## Architecture

The flasher is a fully static single-page application:

- `index.html` — pixel-themed UI (Press Start 2P headers, Pixelify Sans body)
- `assets/app.js` — GitHub API integration, channel selection, esptool-js flashing
- `assets/styles.css` — pixel theme matching sigurdos.dev
- `assets/vendor/esptool-js-bundle.js` — browser ESP32 flashing library

Firmware binaries are fetched at runtime from GitHub release assets — they are not bundled in this repo.

## How it works

1. **Connect** — browser WebSerial connects to the T-Deck's UART
2. **Choose channel** — pick Stable (latest release) or Beta (latest pre-release)
3. **Flash** — esptool-js downloads the firmware-merged.bin from GitHub and writes it via serial bootloader
4. **Reset** — device reboots into SigurdOS

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
python3 -m http.server 8080
```

Open `http://127.0.0.1:8080`.
