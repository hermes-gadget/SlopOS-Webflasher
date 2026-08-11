# SigurdOS Web Flasher

Browser-based firmware flasher for the LilyGo T-Deck running [SigurdOS](https://github.com/hermes-gadget/SigurdOS-tdeck).

Flash firmware directly from the browser over WebSerial. No tools, no accounts, no downloads required.

## Features

- **Zero-install** — open the page, connect your T-Deck, and flash
- **Stable & Beta channels** — pick your firmware version from GitHub releases
- **Publisher-authenticated** — an Ed25519-signed manifest is verified before vault promotion and again in-browser
- **Pixel theme** — matches the sigurdos.dev design language
- **Manifest-defined layout** — signed offsets, sizes, digests, partition layout, and ESP32-S3 headers are validated before flashing
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

The compose deployment runs nginx and the Python API as separate services. Put a
populated vault in `./vault` (or set `SIGURDOS_FIRMWARE_VAULT` to another host
directory); nginx proxies `/api/*`, `/dev/*`, `/latest/*`, and `/archive/*` to
the backend. The backend mounts the vault read-only, so the web tier cannot
modify firmware.

To populate the vault manually from the public GitHub releases API, run the
least-privileged sync profile against a writable vault:

```bash
docker compose --profile sync run --rm firmware-sync
```

Production deployments should schedule that one-shot service outside the web
stack (for example, a host timer or orchestrator job), keep the vault volume
writable only for `firmware-sync`, and expose only the nginx port. The sync
job, vault provisioning, TLS termination, and scheduler remain deployment
infrastructure responsibilities.

## Architecture

The flasher is a small Python backend (`server.py`) serving a static single-page frontend:

- `server.py` — HTTP server: exact static-file allowlist, firmware vault, `/api/releases`, path-traversal protection, strict security headers
- `sync_firmware.py` — promotes only signed, schema-valid T-Deck release binaries into the local vault
- `firmware_manifest.py` — shared T-Deck/ESP32-S3 manifest, partition, image-header, size, offset, and digest contract
- `sign_firmware_manifest.py` — trusted-CI helper for creating `firmware-manifest.json` and its detached signature
- `nginx.conf` + `nginx-security-headers.conf` — static frontend, backend proxy, and security headers (Docker)
- `Dockerfile.backend` — unprivileged Python API/sync image
- `index.html` — pixel-themed UI (Press Start 2P headers, Pixelify Sans body)
- `assets/app.js` + `assets/firmware-security.js` — channel selection, signature/schema/image verification, and esptool-js flashing
- `assets/styles.css` — pixel theme matching sigurdos.dev
- `assets/vendor/esptool-js-bundle.js` — browser ESP32 flashing library

Firmware binaries are synced from GitHub release assets into a local vault — they are not bundled in this repo.

## How it works

1. **Connect** — browser WebSerial connects to the T-Deck's UART
2. **Choose channel** — pick Stable (latest release) or Beta (latest pre-release)
3. **Authenticate** — verifies the release manifest with the public key pinned in the browser
4. **Validate** — downloads the manifest-selected images and checks sizes, SHA-256 digests, ESP32-S3 headers, and partition layout
5. **Identify** — esptool must report exactly `ESP32-S3`; Full Erase also requires typing `ERASE T-DECK`
6. **Flash** — esptool-js writes only the signed manifest's offsets via the serial bootloader
7. **Reset** — device reboots into SigurdOS

## Requirements

- Chrome, Edge, or Opera (WebSerial API)
- HTTPS or localhost
- LilyGo T-Deck (ESP32-S3) with USB connected
- Firmware release on GitHub with `firmware-merged.bin`, `firmware.bin`, `firmware-debug.bin`, `firmware-manifest.json`, and `firmware-manifest.sig`

Unsigned legacy releases intentionally fail closed and cannot become `latest`, `dev`, or `debug`.

## Release signing

The release workflow must keep the Ed25519 private key in trusted CI secrets, outside this repository and outside the firmware vault. After producing the three firmware images, create the immutable manifest and detached signature with:

```bash
python3 sign_firmware_manifest.py \
  --artifacts-dir artifacts \
  --release "$GITHUB_REF_NAME" \
  --private-key "$RUNNER_TEMP/firmware-manifest-ed25519.pem"
```

The helper validates the merged and application images before signing and fails if the CI key does not match [firmware-signing-public-key.pem](firmware-signing-public-key.pem). The pinned public-key DER SHA-256 fingerprint is `a71e3378758d749de3b440bd27a92776fdcd3787a06869a3c3c1b015feb9e722`. Never commit or upload the private key as a release artifact.

This protects against release-asset or vault substitution. A fully compromised web origin could replace the browser verifier itself, so production devices should additionally enforce Espressif Secure Boot/signed firmware or load the verifier from a separately trusted immutable origin.

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

The development server binds only to loopback by default. An intentional network deployment can pass a host explicitly (`python3 server.py 8082 0.0.0.0`) or set `SIGURDOS_HOST`, and should remain behind the production reverse proxy.
