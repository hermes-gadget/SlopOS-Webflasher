"""Signed firmware-manifest contract shared by the sync job and signing CI.

The detached signature covers the exact UTF-8 bytes of ``firmware-manifest.json``.
Only the public verification key is stored in this repository.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import struct
import subprocess
import tempfile
from pathlib import Path


SCHEMA_VERSION = 1
BOARD_NAME = "LILYGO T-Deck"
CHIP_NAME = "ESP32-S3"
FLASH_SIZE = 16 * 1024 * 1024
PARTITION_TABLE_NAME = "partitions_sigurdos_16MB.csv"
PARTITION_TABLE_OFFSET = 0x8000
PARTITION_TABLE_SIZE = 0x1000
BOOT_APP0_OFFSET = 0xE000
APP_OFFSET = 0x10000
ESP_IMAGE_MAGIC = 0xE9
ESP32S3_CHIP_ID = 9
MAX_IMAGE_SEGMENTS = 16
MAX_MANIFEST_SIZE = 64 * 1024
MAX_SIGNATURE_SIZE = 4 * 1024

MANIFEST_FILENAME = "firmware-manifest.json"
SIGNATURE_FILENAME = "firmware-manifest.sig"
PUBLIC_KEY_PATH = Path(__file__).with_name("firmware-signing-public-key.pem")

EXPECTED_MODES = {
    "full": ("firmware-merged.bin", 0, "merged"),
    "update": ("firmware.bin", APP_OFFSET, "application"),
    "debug": ("firmware-debug.bin", 0, "merged"),
}


class ManifestError(ValueError):
    """Raised when signed firmware metadata or referenced images are unsafe."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_manifest_bytes(manifest: dict) -> bytes:
    return (
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _exact_keys(value: dict, expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ManifestError(f"{label} has unsupported or missing fields")


def _plain_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(f"{label} must be an integer")
    return value


def _safe_filename(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{label} must be a filename")
    if Path(value).name != value or value.startswith("."):
        raise ManifestError(f"{label} must be a non-hidden local filename")
    if not all(ch.isalnum() or ch in "-_." for ch in value):
        raise ManifestError(f"{label} contains unsafe characters")
    return value


def validate_manifest_data(manifest: object, expected_release: str | None = None) -> dict:
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be a JSON object")
    _exact_keys(
        manifest,
        {
            "schema_version",
            "release",
            "board",
            "chip",
            "flash_size",
            "partition_layout",
            "modes",
        },
        "manifest",
    )
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError("unsupported firmware manifest schema")
    release = manifest.get("release")
    if not isinstance(release, str) or not release or len(release) > 128:
        raise ManifestError("release must be a non-empty bounded string")
    if expected_release is not None and release != expected_release:
        raise ManifestError(
            f"manifest release {release!r} does not match GitHub release {expected_release!r}"
        )
    if manifest.get("board") != BOARD_NAME or manifest.get("chip") != CHIP_NAME:
        raise ManifestError("manifest is not bound to the LILYGO T-Deck / ESP32-S3")
    if manifest.get("flash_size") != FLASH_SIZE:
        raise ManifestError("manifest flash size is not the T-Deck 16 MB layout")

    layout = manifest.get("partition_layout")
    if not isinstance(layout, dict):
        raise ManifestError("partition_layout must be an object")
    _exact_keys(layout, {"name", "table_offset", "app_offset", "partitions"}, "partition_layout")
    if layout.get("name") != PARTITION_TABLE_NAME:
        raise ManifestError("unexpected T-Deck partition layout name")
    if layout.get("table_offset") != PARTITION_TABLE_OFFSET:
        raise ManifestError("partition table must be flashed at 0x8000")
    if layout.get("app_offset") != APP_OFFSET:
        raise ManifestError("application partition must begin at 0x10000")

    partitions = layout.get("partitions")
    if not isinstance(partitions, list) or not partitions:
        raise ManifestError("partition_layout.partitions must be a non-empty list")
    normalized_partitions = []
    labels = set()
    for index, part in enumerate(partitions):
        if not isinstance(part, dict):
            raise ManifestError(f"partition {index} must be an object")
        _exact_keys(part, {"label", "type", "subtype", "offset", "size"}, f"partition {index}")
        label = part.get("label")
        if not isinstance(label, str) or not label or len(label) > 16 or not label.isascii():
            raise ManifestError(f"partition {index} has an invalid label")
        if label in labels:
            raise ManifestError(f"partition label {label!r} is repeated")
        labels.add(label)
        partition_type = _plain_int(part.get("type"), f"partition {label} type")
        subtype = _plain_int(part.get("subtype"), f"partition {label} subtype")
        offset = _plain_int(part.get("offset"), f"partition {label} offset")
        size = _plain_int(part.get("size"), f"partition {label} size")
        if not 0 <= partition_type <= 0xFF or not 0 <= subtype <= 0xFF:
            raise ManifestError(f"partition {label} has an invalid type")
        if offset < 0 or size <= 0 or offset + size > FLASH_SIZE:
            raise ManifestError(f"partition {label} exceeds the T-Deck flash bounds")
        normalized_partitions.append(
            {"label": label, "type": partition_type, "subtype": subtype, "offset": offset, "size": size}
        )
    if normalized_partitions != sorted(normalized_partitions, key=lambda part: part["offset"]):
        raise ManifestError("partitions must be ordered by offset")
    for previous, current in zip(normalized_partitions, normalized_partitions[1:]):
        if current["offset"] < previous["offset"] + previous["size"]:
            raise ManifestError("partition layout contains overlapping ranges")
    app_partitions = [
        part for part in normalized_partitions
        if part["type"] == 0 and part["offset"] == APP_OFFSET
    ]
    if len(app_partitions) != 1:
        raise ManifestError("partition layout must contain one application at 0x10000")

    modes = manifest.get("modes")
    if not isinstance(modes, dict) or set(modes) != set(EXPECTED_MODES):
        raise ManifestError("manifest must define full, update, and debug modes")
    seen_files = set()
    for mode, (expected_file, expected_offset, expected_kind) in EXPECTED_MODES.items():
        images = modes.get(mode)
        if not isinstance(images, list) or len(images) != 1:
            raise ManifestError(f"{mode} mode must contain exactly one image")
        image = images[0]
        if not isinstance(image, dict):
            raise ManifestError(f"{mode} image must be an object")
        _exact_keys(image, {"file", "offset", "size", "sha256", "kind"}, f"{mode} image")
        filename = _safe_filename(image.get("file"), f"{mode} image file")
        offset = _plain_int(image.get("offset"), f"{mode} image offset")
        size = _plain_int(image.get("size"), f"{mode} image size")
        digest = image.get("sha256")
        if (
            filename != expected_file
            or offset != expected_offset
            or image.get("kind") != expected_kind
        ):
            raise ManifestError(f"{mode} mode does not match the signed T-Deck flash contract")
        if size <= 0 or offset < 0 or offset + size > FLASH_SIZE:
            raise ManifestError(f"{mode} image exceeds T-Deck flash bounds")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ManifestError(f"{mode} image needs a SHA-256 digest")
        try:
            bytes.fromhex(digest)
        except ValueError as exc:
            raise ManifestError(f"{mode} image has an invalid SHA-256 digest") from exc
        if digest != digest.lower():
            raise ManifestError(f"{mode} image digest must use lowercase hex")
        if filename in seen_files:
            raise ManifestError(f"firmware file {filename!r} is repeated")
        seen_files.add(filename)
    return manifest


def load_manifest_bytes(data: bytes, expected_release: str | None = None) -> dict:
    if not data or len(data) > MAX_MANIFEST_SIZE:
        raise ManifestError("firmware manifest is empty or too large")
    try:
        text = data.decode("utf-8", errors="strict")
        manifest = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"firmware manifest is not valid UTF-8 JSON: {exc}") from exc
    return validate_manifest_data(manifest, expected_release)


def verify_manifest_signature(
    manifest_path: Path,
    signature_path: Path,
    public_key_path: Path = PUBLIC_KEY_PATH,
) -> None:
    try:
        encoded = signature_path.read_bytes().strip()
    except OSError as exc:
        raise ManifestError(f"cannot read detached manifest signature: {exc}") from exc
    if not encoded or len(encoded) > MAX_SIGNATURE_SIZE:
        raise ManifestError("detached manifest signature is empty or too large")
    try:
        signature = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ManifestError("detached manifest signature is not valid base64") from exc
    if len(signature) != 64:
        raise ManifestError("detached Ed25519 signature must be 64 bytes")

    with tempfile.NamedTemporaryFile(prefix="sigurdos-manifest-", suffix=".sig") as raw_signature:
        raw_signature.write(signature)
        raw_signature.flush()
        try:
            result = subprocess.run(
                [
                    "openssl",
                    "pkeyutl",
                    "-verify",
                    "-pubin",
                    "-inkey",
                    os.fspath(public_key_path),
                    "-rawin",
                    "-in",
                    os.fspath(manifest_path),
                    "-sigfile",
                    raw_signature.name,
                ],
                capture_output=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ManifestError(f"OpenSSL could not verify the firmware manifest: {exc}") from exc
    if result.returncode != 0:
        raise ManifestError("firmware manifest signature does not match the pinned release key")


def _align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def validate_esp32s3_image(data: bytes, offset: int) -> int:
    header_size = 24
    if offset < 0 or offset + header_size > len(data):
        raise ManifestError(f"ESP image header at 0x{offset:x} is truncated")
    magic, segment_count, _, _, _ = struct.unpack_from("<BBBBI", data, offset)
    if magic != ESP_IMAGE_MAGIC:
        raise ManifestError(f"ESP image at 0x{offset:x} has invalid magic")
    if not 1 <= segment_count <= MAX_IMAGE_SEGMENTS:
        raise ManifestError("ESP image has an invalid segment count")
    extended = struct.unpack_from("<BBBBHBHHBBBBB", data, offset + 8)
    if extended[4] != ESP32S3_CHIP_ID:
        raise ManifestError("ESP image header does not target ESP32-S3")
    append_digest = extended[-1]
    if append_digest not in (0, 1):
        raise ManifestError("ESP image has an invalid append-digest flag")

    position = offset + header_size
    checksum = 0xEF
    for index in range(segment_count):
        if position + 8 > len(data):
            raise ManifestError(f"ESP image segment {index} header is truncated")
        _, segment_size = struct.unpack_from("<II", data, position)
        if segment_size == 0 or segment_size % 4:
            raise ManifestError(f"ESP image segment {index} has an invalid size")
        position += 8
        if position + segment_size > len(data):
            raise ManifestError(f"ESP image segment {index} data is truncated")
        for value in data[position : position + segment_size]:
            checksum ^= value
        position += segment_size

    checksum_end = _align_up(position + 1, 16)
    checksum_position = checksum_end - 1
    if checksum_position >= len(data) or data[checksum_position] != checksum:
        raise ManifestError("ESP image checksum is missing or invalid")
    image_end = checksum_end + (32 if append_digest else 0)
    if image_end > len(data):
        raise ManifestError("ESP image digest is truncated")
    if append_digest:
        expected = hashlib.sha256(data[offset:checksum_end]).digest()
        if data[checksum_end:image_end] != expected:
            raise ManifestError("ESP image embedded SHA-256 digest is invalid")
    return image_end - offset


def parse_partition_table(data: bytes, table_offset: int = PARTITION_TABLE_OFFSET) -> list[dict]:
    if table_offset + PARTITION_TABLE_SIZE > len(data):
        raise ManifestError("merged image is truncated before the partition table")
    entries = []
    md5_verified = False
    position = table_offset
    table_end = table_offset + PARTITION_TABLE_SIZE
    while position + 32 <= table_end:
        raw = data[position : position + 32]
        magic = struct.unpack_from("<H", raw)[0]
        if magic == 0xFFFF:
            break
        if magic == 0xEBEB:
            if raw[2:16] != b"\xff" * 14:
                raise ManifestError("partition-table MD5 record has invalid padding")
            expected = hashlib.md5(data[table_offset:position]).digest()  # nosec: ESP-IDF format
            if raw[16:] != expected:
                raise ManifestError("partition-table MD5 does not match its entries")
            md5_verified = True
            break
        if magic != 0x50AA:
            raise ManifestError(f"partition table has invalid magic at 0x{position:x}")
        _, partition_type, subtype, offset, size, raw_label, _ = struct.unpack("<HBBII16sI", raw)
        try:
            label = raw_label.split(b"\0", 1)[0].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ManifestError("partition table has a non-ASCII label") from exc
        if not label or size <= 0 or offset + size > FLASH_SIZE:
            raise ManifestError("partition table contains an invalid range")
        entries.append(
            {"label": label, "type": partition_type, "subtype": subtype, "offset": offset, "size": size}
        )
        position += 32
    if not entries or not md5_verified:
        raise ManifestError("partition table is missing entries or its ESP-IDF MD5 record")
    entries.sort(key=lambda part: part["offset"])
    for previous, current in zip(entries, entries[1:]):
        if current["offset"] < previous["offset"] + previous["size"]:
            raise ManifestError("partition table contains overlapping ranges")
    return entries


def validate_image_bytes(data: bytes, image: dict, manifest: dict) -> None:
    if len(data) != image["size"]:
        raise ManifestError(f"{image['file']} size does not match its signed manifest entry")
    if hashlib.sha256(data).hexdigest() != image["sha256"]:
        raise ManifestError(f"{image['file']} digest does not match its signed manifest entry")
    if image["kind"] == "application":
        validate_esp32s3_image(data, 0)
        return

    validate_esp32s3_image(data, 0)
    partitions = parse_partition_table(data, manifest["partition_layout"]["table_offset"])
    if partitions != manifest["partition_layout"]["partitions"]:
        raise ManifestError(f"{image['file']} partition table differs from the signed layout")
    if len(data) < APP_OFFSET or struct.unpack_from("<I", data, BOOT_APP0_OFFSET)[0] != 1:
        raise ManifestError(f"{image['file']} does not contain the required boot_app0 component")
    validate_esp32s3_image(data, manifest["partition_layout"]["app_offset"])


def validate_release_files(manifest: dict, directory: Path) -> None:
    validate_manifest_data(manifest)
    for mode in EXPECTED_MODES:
        image = manifest["modes"][mode][0]
        path = directory / image["file"]
        if path.is_symlink() or not path.is_file():
            raise ManifestError(f"signed firmware image {image['file']} is missing or is a symlink")
        if path.stat().st_size != image["size"]:
            raise ManifestError(f"{image['file']} size does not match its signed manifest entry")
        if sha256_file(path) != image["sha256"]:
            raise ManifestError(f"{image['file']} digest does not match its signed manifest entry")
        data = path.read_bytes()
        validate_image_bytes(data, image, manifest)


def build_manifest(directory: Path, release: str) -> dict:
    full_path = directory / EXPECTED_MODES["full"][0]
    if full_path.is_symlink() or not full_path.is_file():
        raise ManifestError(f"missing release image {full_path.name}")
    full_data = full_path.read_bytes()
    partitions = parse_partition_table(full_data)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "release": release,
        "board": BOARD_NAME,
        "chip": CHIP_NAME,
        "flash_size": FLASH_SIZE,
        "partition_layout": {
            "name": PARTITION_TABLE_NAME,
            "table_offset": PARTITION_TABLE_OFFSET,
            "app_offset": APP_OFFSET,
            "partitions": partitions,
        },
        "modes": {},
    }
    for mode, (filename, offset, kind) in EXPECTED_MODES.items():
        path = directory / filename
        if path.is_symlink() or not path.is_file():
            raise ManifestError(f"missing release image {filename}")
        manifest["modes"][mode] = [
            {
                "file": filename,
                "offset": offset,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
                "kind": kind,
            }
        ]
    validate_release_files(validate_manifest_data(manifest, release), directory)
    return manifest
