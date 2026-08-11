#!/usr/bin/env python3
"""Create and sign the immutable T-Deck firmware manifest in trusted release CI."""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from firmware_manifest import (
    MANIFEST_FILENAME,
    PUBLIC_KEY_PATH,
    SIGNATURE_FILENAME,
    ManifestError,
    build_manifest,
    canonical_manifest_bytes,
    verify_manifest_signature,
)


def sign_manifest(manifest_path: Path, signature_path: Path, private_key: Path) -> None:
    if private_key.is_symlink() or not private_key.is_file():
        raise ManifestError("release signing key is missing or is a symlink")
    with tempfile.NamedTemporaryFile(prefix="sigurdos-manifest-", suffix=".sig") as raw_signature:
        result = subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-inkey",
                os.fspath(private_key),
                "-rawin",
                "-in",
                os.fspath(manifest_path),
                "-out",
                raw_signature.name,
            ],
            capture_output=True,
            check=False,
            timeout=15,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise ManifestError(f"OpenSSL could not sign the firmware manifest: {detail}")
        raw_signature.seek(0)
        signature = raw_signature.read()
    if len(signature) != 64:
        raise ManifestError("OpenSSL did not produce a 64-byte Ed25519 signature")
    signature_path.write_bytes(base64.b64encode(signature) + b"\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--release", required=True, help="immutable Git tag/version")
    parser.add_argument(
        "--private-key",
        type=Path,
        required=True,
        help="Ed25519 private key injected from CI secrets; never place it in artifacts",
    )
    parser.add_argument("--public-key", type=Path, default=PUBLIC_KEY_PATH)
    args = parser.parse_args()

    try:
        directory = args.artifacts_dir.resolve(strict=True)
        manifest = build_manifest(directory, args.release)
        manifest_path = directory / MANIFEST_FILENAME
        signature_path = directory / SIGNATURE_FILENAME
        manifest_path.write_bytes(canonical_manifest_bytes(manifest))
        sign_manifest(manifest_path, signature_path, args.private_key)
        # Fail CI if its secret key is not the private half of the browser-pinned key.
        verify_manifest_signature(manifest_path, signature_path, args.public_key)
    except (ManifestError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"firmware manifest signing failed: {exc}", file=sys.stderr)
        return 1

    print(f"signed {manifest_path} with the pinned SigurdOS release key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
