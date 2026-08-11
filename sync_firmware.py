#!/usr/bin/env python3
"""Mirror only signed, schema-validated SigurdOS T-Deck releases into the vault."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from firmware_manifest import (
    MANIFEST_FILENAME,
    MAX_MANIFEST_SIZE,
    MAX_SIGNATURE_SIZE,
    PUBLIC_KEY_PATH,
    SIGNATURE_FILENAME,
    ManifestError,
    load_manifest_bytes,
    sha256_file,
    validate_release_files,
    verify_manifest_signature,
)


VAULT = Path(
    os.environ.get("SIGURDOS_FIRMWARE_VAULT", "~/firmware/vault")
).expanduser().resolve()
GITHUB_OWNER = "hermes-gadget"
GITHUB_REPO = "SigurdOS-tdeck"
STATE_FILE = VAULT / "state.json"
USER_AGENT = "SigurdOS-FirmwareSync/2.0"
SAFE_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [sync] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("firmware-sync")


def github_request(path: str) -> dict | list:
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/{path}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github.v3+json"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 403 and attempt < 2:
                log.warning("Rate limited, sleeping 30s...")
                time.sleep(30)
                continue
            raise
        except (urllib.error.URLError, OSError) as exc:
            if attempt < 2:
                log.warning("Network error %s, retrying in 10s...", exc)
                time.sleep(10)
                continue
            raise
    raise RuntimeError(f"failed to fetch {path} after 3 attempts")


def _asset_map(release: dict) -> dict[str, dict]:
    result = {}
    for asset in release.get("assets", []):
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        if not isinstance(name, str) or Path(name).name != name or name.startswith("."):
            continue
        if name in result:
            raise ManifestError(f"GitHub release repeats asset {name!r}")
        result[name] = asset
    return result


def download_asset(
    asset: dict,
    destination: Path,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    maximum_size: int,
) -> None:
    """Stream one release asset into a bounded temporary file, then promote it."""
    url = asset.get("url")
    if not isinstance(url, str) or not url.startswith("https://api.github.com/"):
        raise ManifestError(f"{destination.name}: release asset URL is not a GitHub API URL")
    metadata_size = asset.get("size")
    if expected_size is not None and metadata_size != expected_size:
        raise ManifestError(f"{destination.name}: GitHub size differs from signed size")
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/octet-stream", "User-Agent": USER_AGENT},
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw_length = response.headers.get("Content-Length")
            if raw_length is None:
                raise ManifestError(f"{destination.name}: missing Content-Length")
            try:
                content_length = int(raw_length)
            except ValueError as exc:
                raise ManifestError(f"{destination.name}: invalid Content-Length") from exc
            required_size = expected_size if expected_size is not None else content_length
            if content_length != required_size or content_length <= 0 or content_length > maximum_size:
                raise ManifestError(f"{destination.name}: response size is outside signed bounds")

            digest = hashlib.sha256()
            total = 0
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{destination.name}.", dir=destination.parent, delete=False
            ) as output:
                temp_path = Path(output.name)
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > required_size or total > maximum_size:
                        raise ManifestError(f"{destination.name}: download exceeded signed size")
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if total != required_size:
                raise ManifestError(f"{destination.name}: truncated download")
            if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
                raise ManifestError(f"{destination.name}: SHA-256 differs from signed manifest")
        os.replace(temp_path, destination)
        temp_path = None
    except (urllib.error.URLError, OSError) as exc:
        raise ManifestError(f"{destination.name}: download failed: {exc}") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _load_valid_release(directory: Path, expected_release: str) -> dict:
    manifest_path = directory / MANIFEST_FILENAME
    signature_path = directory / SIGNATURE_FILENAME
    if manifest_path.is_symlink() or signature_path.is_symlink():
        raise ManifestError("signed manifest files must not be symlinks")
    verify_manifest_signature(manifest_path, signature_path, PUBLIC_KEY_PATH)
    manifest = load_manifest_bytes(manifest_path.read_bytes(), expected_release)
    validate_release_files(manifest, directory)
    return manifest


def stage_release(release: dict, channel: str, api_ok: bool) -> tuple[Path, dict] | None:
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not SAFE_TAG_RE.fullmatch(tag):
        raise ManifestError("release tag contains unsafe characters")
    archive_parent = VAULT / "archive" / channel
    archive_dir = archive_parent / tag
    if archive_dir.is_dir():
        try:
            return archive_dir, _load_valid_release(archive_dir, tag)
        except ManifestError as exc:
            if not api_ok:
                raise
            log.warning("Existing %s is not trusted and will be replaced: %s", tag, exc)
    if not api_ok:
        return None

    assets = _asset_map(release)
    manifest_asset = assets.get(MANIFEST_FILENAME)
    signature_asset = assets.get(SIGNATURE_FILENAME)
    if manifest_asset is None or signature_asset is None:
        raise ManifestError("release has no signed firmware manifest")

    archive_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{tag}.staging-", dir=archive_parent))
    quarantine = None
    try:
        manifest_path = staging / MANIFEST_FILENAME
        signature_path = staging / SIGNATURE_FILENAME
        download_asset(manifest_asset, manifest_path, maximum_size=MAX_MANIFEST_SIZE)
        download_asset(signature_asset, signature_path, maximum_size=MAX_SIGNATURE_SIZE)

        # Authentication happens before any binary can enter a live archive/channel.
        verify_manifest_signature(manifest_path, signature_path, PUBLIC_KEY_PATH)
        manifest = load_manifest_bytes(manifest_path.read_bytes(), tag)
        for mode in ("full", "update", "debug"):
            image = manifest["modes"][mode][0]
            asset = assets.get(image["file"])
            if asset is None:
                raise ManifestError(f"release is missing signed image {image['file']}")
            download_asset(
                asset,
                staging / image["file"],
                expected_size=image["size"],
                expected_sha256=image["sha256"],
                maximum_size=manifest["flash_size"],
            )
        validate_release_files(manifest, staging)

        if archive_dir.exists():
            quarantine = archive_parent / f".{tag}.rejected-{uuid.uuid4().hex}"
            os.replace(archive_dir, quarantine)
        os.replace(staging, archive_dir)
        if quarantine is not None:
            shutil.rmtree(quarantine)
            quarantine = None
        log.info("Promoted authenticated release %s (%s)", tag, channel)
        return archive_dir, manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if quarantine is not None and quarantine.exists() and not archive_dir.exists():
            os.replace(quarantine, archive_dir)


def scan_local_vault() -> list[dict]:
    releases = []
    archive_root = VAULT / "archive"
    if not archive_root.is_dir():
        return releases
    for channel in ("stable", "beta"):
        channel_path = archive_root / channel
        if not channel_path.is_dir():
            continue
        directories = sorted(
            (path for path in channel_path.iterdir() if path.is_dir() and SAFE_TAG_RE.fullmatch(path.name)),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in directories:
            releases.append(
                {
                    "tag_name": path.name,
                    "prerelease": channel == "beta",
                    "published_at": "",
                    "assets": [],
                }
            )
    return releases


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix=f".{path.name}.", dir=path.parent, delete=False
    ) as output:
        temporary = Path(output.name)
        json.dump(value, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _replace_symlink(link: Path, target: Path) -> None:
    if link.exists() and not link.is_symlink():
        raise ManifestError(f"refusing to replace non-symlink channel path {link}")
    temporary = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
    temporary.symlink_to(os.path.relpath(target, link.parent))
    os.replace(temporary, link)


def _release_metadata(release: dict, directory: Path, manifest: dict) -> dict:
    filenames = [MANIFEST_FILENAME, SIGNATURE_FILENAME]
    filenames.extend(manifest["modes"][mode][0]["file"] for mode in ("full", "update", "debug"))
    assets = []
    for filename in filenames:
        path = directory / filename
        assets.append({"name": filename, "size": path.stat().st_size, "sha256": sha256_file(path)})
    return {
        "tag_name": manifest["release"],
        "prerelease": bool(release.get("prerelease", True)),
        "published_at": release.get("published_at", ""),
        "assets": assets,
    }


def sync_releases() -> None:
    VAULT.mkdir(parents=True, exist_ok=True)
    log.info("Fetching releases from GitHub...")
    try:
        releases = github_request("releases?per_page=20")
        if not isinstance(releases, list) or not releases:
            raise ValueError("unexpected response or no releases")
        api_ok = True
    except Exception as exc:
        log.warning("GitHub API failed (%s). Validating the local vault only.", exc)
        releases = scan_local_vault()
        api_ok = False

    validated = []
    latest_beta = None
    latest_stable = None
    for release in releases:
        tag = release.get("tag_name", "<invalid>")
        channel = "beta" if release.get("prerelease", True) else "stable"
        try:
            staged = stage_release(release, channel, api_ok)
            if staged is None:
                continue
            directory, manifest = staged
        except (ManifestError, OSError) as exc:
            log.error("Rejected release %s: %s", tag, exc)
            continue
        validated.append(_release_metadata(release, directory, manifest))
        if channel == "beta" and latest_beta is None:
            latest_beta = directory
        if channel == "stable" and latest_stable is None:
            latest_stable = directory

    # GitHub returns newest releases first; only authenticated releases can become aliases.
    if latest_beta is not None:
        _replace_symlink(VAULT / "dev", latest_beta)
        _replace_symlink(VAULT / "debug", latest_beta)
    if latest_stable is not None:
        _replace_symlink(VAULT / "latest", latest_stable)
    elif latest_beta is not None:
        _replace_symlink(VAULT / "latest", latest_beta)

    _atomic_json(VAULT / "releases.json", validated)
    state = {
        "schema_version": 2,
        "last_sync": time.time(),
        "validated_releases": [release["tag_name"] for release in validated],
    }
    _atomic_json(STATE_FILE, state)
    log.info("Sync complete. %d authenticated release(s) available.", len(validated))


if __name__ == "__main__":
    try:
        sync_releases()
    except Exception:
        log.exception("Sync failed")
        sys.exit(1)
