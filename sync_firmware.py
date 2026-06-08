#!/usr/bin/env python3
"""
SigurdOS Firmware Sync — watches GitHub releases, pulls assets to local vault.

Runs every 5 minutes via cron (or systemd timer). Idempotent — safe to
re-run. Archives every release version, then creates symlinks:
  dev/     → latest prerelease
  latest/ → latest stable (or dev if no stable exists)

State is persisted in vault/state.json so we only fetch new releases.
"""

import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request

# ── config ──────────────────────────────────────────────
VAULT = os.path.expanduser("~/firmware/vault")
GITHUB_OWNER = "hermes-gadget"
GITHUB_REPO = "SigurdOS-tdeck"
STATE_FILE = os.path.join(VAULT, "state.json")
USER_AGENT = "SigurdOS-FirmwareSync/1.0"

# Files we care about (sorted by preference)
ASSET_PATTERNS = [
    re.compile(r"^firmware-merged\.bin$"),
    re.compile(r"^firmware\.bin$"),
    re.compile(r"^manifest\.json$"),
    re.compile(r"^sigurdos-tdeck-(.+)\.(bin|json)$"),
    re.compile(r"^bootloader\.bin$"),
    re.compile(r"^partitions\.bin$"),
]

# ── setup ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [sync] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("firmware-sync")

os.makedirs(VAULT, exist_ok=True)


# ── helpers ─────────────────────────────────────────────
def github_request(path: str) -> dict | list:
    """Fetch JSON from GitHub API with retry."""
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/{path}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github.v3+json",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            return json.loads(data)
        except urllib.error.HTTPError as e:
            if e.code == 403 and attempt < 2:
                log.warning("Rate limited, sleeping 30s...")
                time.sleep(30)
                continue
            raise
        except (urllib.error.URLError, OSError) as e:
            if attempt < 2:
                log.warning(f"Network error {e}, retrying in 10s...")
                time.sleep(10)
                continue
            raise
    raise RuntimeError(f"Failed to fetch {path} after 3 attempts")


def download_asset(url: str, dest: str) -> bool:
    """Download a file from GitHub to dest. Returns True if changed."""
    tmp = dest + ".tmp"
    req = urllib.request.Request(url, headers={
        "Accept": "application/octet-stream",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
    except Exception as e:
        log.error(f"Download failed: {url[:60]}… — {e}")
        return False

    # Check SHA256 to avoid unnecessary writes
    new_hash = hashlib.sha256(data).hexdigest()
    if os.path.exists(dest):
        with open(dest, "rb") as f:
            existing_hash = hashlib.sha256(f.read()).hexdigest()
        if existing_hash == new_hash:
            return False  # unchanged

    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    log.info(f"  Downloaded {os.path.basename(dest)} ({len(data)} bytes, sha256={new_hash[:12]}…)")
    return True


def is_interesting_asset(name: str) -> bool:
    """Check if we should archive this asset."""
    return any(p.match(name) for p in ASSET_PATTERNS)


def safe_asset_name(name: str, tag: str) -> str:
    """Normalize asset filenames to a consistent scheme."""
    # If it already has the tag prefix, keep as-is (e.g. sigurdos-tdeck-firmware.bin)
    # Otherwise use the original name (e.g. firmware-merged.bin)
    if name.startswith("sigurdos-tdeck-") or name.startswith("slopos-tdeck-"):
        # Strip old brand prefix
        return re.sub(r"^(sigurdos|slopos)-tdeck-", "", name)
    return name


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt state.json, starting fresh")
    return {"last_checked_release": None, "downloaded": {}}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def atomic_symlink(target: str, link: str):
    """Create/update a symlink atomically."""
    tmp = link + ".tmp"
    os.symlink(target, tmp)
    os.replace(tmp, link)


def get_channel_dir(tag: str, is_prerelease: bool) -> str:
    """Determine which channel directory a release belongs to."""
    return "beta" if is_prerelease else "stable"


# ── main sync logic ─────────────────────────────────────
def scan_local_vault() -> list:
    """
    Scan local vault directories to build release metadata without GitHub API.

    Returns the releases data structure (list of dicts with tag_name, prerelease,
    assets) that would normally come from the GitHub API.
    """
    releases = []
    archive_dir = os.path.join(VAULT, "archive")
    if not os.path.isdir(archive_dir):
        return releases

    for channel in sorted(os.listdir(archive_dir), reverse=True):
        channel_path = os.path.join(archive_dir, channel)
        if not os.path.isdir(channel_path):
            continue
        for tag in sorted(os.listdir(channel_path), reverse=True):
            tag_path = os.path.join(channel_path, tag)
            if not os.path.isdir(tag_path):
                continue
            assets = []
            for fname in sorted(os.listdir(tag_path)):
                fpath = os.path.join(tag_path, fname)
                if os.path.isfile(fpath):
                    assets.append({
                        "name": fname,
                        "size": os.path.getsize(fpath),
                    })
            releases.append({
                "tag_name": tag,
                "prerelease": channel == "beta",
                "published_at": "",  # unknown from local scan
                "assets": assets,
            })
    return releases


def sync_releases():
    state = load_state()
    last_checked = state.get("last_checked_release")
    downloaded = state.get("downloaded", {})

    log.info("Fetching releases from GitHub...")
    try:
        releases = github_request("releases?per_page=20")
        if not isinstance(releases, list) or len(releases) == 0:
            raise ValueError("Unexpected response or no releases")
        log.info(f"Got {len(releases)} releases from GitHub API")
        api_ok = True
    except Exception as e:
        log.warning(f"GitHub API failed ({e}). Falling back to local vault scan.")
        releases = scan_local_vault()
        api_ok = False
        log.info(f"Found {len(releases)} releases in local vault")

    new_count = 0
    latest_beta = None
    latest_stable = None

    for release in releases:
        tag = release["tag_name"]
        prerelease = release.get("prerelease", True)
        published = release.get("published_at", "unknown")
        assets = release.get("assets", [])
        channel = get_channel_dir(tag, prerelease)

        # Skip if we've already downloaded every asset for this version
        already = downloaded.get(tag, {})
        all_done = all(
            already.get(a["name"], False)
            for a in assets if is_interesting_asset(a["name"])
        ) if assets else False

        if all_done and last_checked and tag == last_checked:
            # This release and everything before it is already processed
            break

        archive_dir = os.path.join(VAULT, "archive", channel, tag)
        os.makedirs(archive_dir, exist_ok=True)
        log.info(f"Processing {tag} ({channel}, published {published})")

        changed = False
        for asset in assets:
            name = asset["name"]
            if not is_interesting_asset(name):
                continue

            clean_name = safe_asset_name(name, tag)
            dest = os.path.join(archive_dir, clean_name)

            if api_ok:
                log.info(f"  Asset: {name} → {clean_name}")
                url = asset["url"]
                ok = download_asset(url, dest)
                if ok:
                    changed = True
            else:
                # Local-only mode: just verify file exists
                if os.path.isfile(dest):
                    log.info(f"  Asset: {name} → {clean_name} (found locally)")
                else:
                    log.warning(f"  Asset: {name} → {clean_name} (MISSING - will fetch when API available)")

            downloaded.setdefault(tag, {})[name] = True

        if changed:
            new_count += 1

        # Track latest per channel
        if channel == "beta" and (latest_beta is None or tag > latest_beta):
            latest_beta = tag
        if channel == "stable" and (latest_stable is None or tag > latest_stable):
            latest_stable = tag

        # Update last_checked (this release is fully processed)
        if last_checked is None or tag > last_checked:
            last_checked = tag

    # ── Update symlinks ──────────────────────────────────
    dev_link = os.path.join(VAULT, "dev")
    latest_link = os.path.join(VAULT, "latest")

    # Helper: replace a file/dir/symlink with a new symlink
    def replace_with_symlink(target_path, link_target_rel):
        if os.path.islink(target_path) or os.path.isfile(target_path):
            os.unlink(target_path)
        elif os.path.isdir(target_path):
            # Try rmdir first (only works if empty)
            try:
                os.rmdir(target_path)
            except OSError:
                # Not empty — move contents aside
                import shutil
                tmp = target_path + "_contents"
                os.rename(target_path, tmp)
                log.warning(f"Moved existing contents of {os.path.basename(target_path)}/ to {tmp}")
        atomic_symlink(link_target_rel, target_path)
        log.info(f"{os.path.basename(target_path)} → {link_target_rel}")

    # dev → latest beta (prerelease)
    if latest_beta:
        beta_archive = os.path.join(VAULT, "archive", "beta", latest_beta)
        rel = os.path.relpath(beta_archive, VAULT)
        replace_with_symlink(dev_link, rel)

    # latest → stable if exists, else dev
    if latest_stable:
        stable_archive = os.path.join(VAULT, "archive", "stable", latest_stable)
        rel = os.path.relpath(stable_archive, VAULT)
        replace_with_symlink(latest_link, rel)
    elif latest_beta:
        replace_with_symlink(latest_link, "dev")

    # ── Write releases.json for the frontend ─────────────
    releases_meta = []
    for release in releases:
        tag = release["tag_name"]
        prerelease = release.get("prerelease", True)
        channel = get_channel_dir(tag, prerelease)
        archive_dir = os.path.join(VAULT, "archive", channel, tag)
        if not os.path.isdir(archive_dir):
            continue
        assets_list = []
        for fname in sorted(os.listdir(archive_dir)):
            fpath = os.path.join(archive_dir, fname)
            if os.path.isfile(fpath):
                assets_list.append({
                    "name": fname,
                    "size": os.path.getsize(fpath),
                })
        releases_meta.append({
            "tag_name": tag,
            "prerelease": prerelease,
            "published_at": release.get("published_at", ""),
            "assets": assets_list,
        })

    meta_path = os.path.join(VAULT, "releases.json")
    with open(meta_path, "w") as f:
        json.dump(releases_meta, f, indent=2)
        f.write("\n")
    log.info(f"Wrote releases.json ({len(releases_meta)} releases)")

    # ── Save state ───────────────────────────────────────
    state["last_checked_release"] = last_checked
    state["downloaded"] = downloaded
    state["last_sync"] = time.time()
    save_state(state)

    log.info(f"Sync complete. {new_count} new/updated release(s). last_checked={last_checked}")


if __name__ == "__main__":
    try:
        sync_releases()
    except Exception as e:
        log.exception("Sync failed")
        sys.exit(1)
