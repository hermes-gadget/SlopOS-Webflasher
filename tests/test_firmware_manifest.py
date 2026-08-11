import base64
import hashlib
import json
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import firmware_manifest
import sign_firmware_manifest
import sync_firmware


def partition_entry(partition_type, subtype, offset, size, label):
    return struct.pack(
        "<HBBII16sI",
        0x50AA,
        partition_type,
        subtype,
        offset,
        size,
        label.encode().ljust(16, b"\0"),
        0,
    )


def esp32s3_image(segment=b"test"):
    common = struct.pack("<BBBBI", 0xE9, 1, 2, 0, 0x40370000)
    extended = struct.pack(
        "<BBBBHBHHBBBBB",
        0xEE, 0, 0, 0, 9, 0, 0, 0, 0, 0, 0, 0, 1,
    )
    image = common + extended + struct.pack("<II", 0x3FC80000, len(segment)) + segment
    checksum = 0xEF
    for value in segment:
        checksum ^= value
    checksum_end = (len(image) + 16) // 16 * 16
    image += b"\0" * (checksum_end - len(image) - 1) + bytes([checksum])
    return image + hashlib.sha256(image).digest()


def write_release(directory: Path):
    app = esp32s3_image()
    bootloader = esp32s3_image(b"boot")
    entries = b"".join(
        [
            partition_entry(1, 0, 0xE000, 0x2000, "otadata"),
            partition_entry(0, 0x10, 0x10000, 0x20000, "app0"),
            partition_entry(1, 0x82, 0x30000, 0x10000, "spiffs"),
        ]
    )
    table = entries + struct.pack("<H", 0xEBEB) + b"\xFF" * 14 + hashlib.md5(entries).digest()  # nosec B324
    merged = bytearray(b"\xFF" * (0x10000 + len(app)))
    merged[: len(bootloader)] = bootloader
    merged[0x8000 : 0x8000 + len(table)] = table
    merged[0xE000 : 0xE000 + 32] = struct.pack("<I", 1) + b"\xFF" * 24 + struct.pack("<I", 0x4743989A)
    merged[0xF000 : 0xF004] = b"\0" * 4
    merged[0x10000:] = app
    (directory / "firmware-merged.bin").write_bytes(merged)
    (directory / "firmware-debug.bin").write_bytes(merged)
    (directory / "firmware.bin").write_bytes(app)


class FirmwareManifestTests(unittest.TestCase):
    def release_dir(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        write_release(directory)
        return directory

    def keypair(self, directory):
        private_key = directory / "private.pem"
        public_key = directory / "public.pem"
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ED25519", "-out", private_key],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["openssl", "pkey", "-in", private_key, "-pubout", "-out", public_key],
            check=True,
            capture_output=True,
        )
        return private_key, public_key

    def test_signed_manifest_authenticates_and_validates_release(self):
        directory = self.release_dir()
        private_key, public_key = self.keypair(directory)
        manifest = firmware_manifest.build_manifest(directory, "beta-test")
        manifest_path = directory / firmware_manifest.MANIFEST_FILENAME
        signature_path = directory / firmware_manifest.SIGNATURE_FILENAME
        manifest_path.write_bytes(firmware_manifest.canonical_manifest_bytes(manifest))
        sign_firmware_manifest.sign_manifest(manifest_path, signature_path, private_key)

        firmware_manifest.verify_manifest_signature(manifest_path, signature_path, public_key)
        loaded = firmware_manifest.load_manifest_bytes(manifest_path.read_bytes(), "beta-test")
        firmware_manifest.validate_release_files(loaded, directory)

    def test_manifest_or_image_tampering_is_rejected(self):
        directory = self.release_dir()
        private_key, public_key = self.keypair(directory)
        manifest = firmware_manifest.build_manifest(directory, "beta-test")
        manifest_path = directory / firmware_manifest.MANIFEST_FILENAME
        signature_path = directory / firmware_manifest.SIGNATURE_FILENAME
        manifest_path.write_bytes(firmware_manifest.canonical_manifest_bytes(manifest))
        sign_firmware_manifest.sign_manifest(manifest_path, signature_path, private_key)

        manifest_path.write_bytes(manifest_path.read_bytes().replace(b"beta-test", b"beta-evil"))
        with self.assertRaisesRegex(firmware_manifest.ManifestError, "pinned release key"):
            firmware_manifest.verify_manifest_signature(manifest_path, signature_path, public_key)

        manifest_path.write_bytes(firmware_manifest.canonical_manifest_bytes(manifest))
        with (directory / "firmware.bin").open("ab") as output:
            output.write(b"tamper")
        with self.assertRaisesRegex(firmware_manifest.ManifestError, "size does not match"):
            firmware_manifest.validate_release_files(manifest, directory)

    def test_schema_rejects_inferred_or_wrong_flash_offsets(self):
        directory = self.release_dir()
        manifest = firmware_manifest.build_manifest(directory, "beta-test")
        manifest["modes"]["update"][0]["offset"] = 0
        with self.assertRaisesRegex(firmware_manifest.ManifestError, "flash contract"):
            firmware_manifest.validate_manifest_data(manifest)

    def test_public_pem_matches_browser_pinned_raw_key(self):
        result = subprocess.run(
            [
                "openssl", "pkey", "-pubin", "-in", firmware_manifest.PUBLIC_KEY_PATH,
                "-outform", "DER",
            ],
            check=True,
            capture_output=True,
        )
        raw_key = base64.b64encode(result.stdout[-32:]).decode()
        browser_source = (Path(__file__).parents[1] / "assets/firmware-security.js").read_text()
        self.assertIn(f"'{raw_key}'", browser_source)


class SyncPromotionTests(unittest.TestCase):
    def test_failed_asset_download_keeps_existing_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "firmware.bin"
            destination.write_bytes(b"known-good")
            asset = {"url": "https://api.github.com/repos/example/assets/1"}
            with mock.patch.object(
                sync_firmware.urllib.request,
                "urlopen",
                side_effect=sync_firmware.urllib.error.URLError("offline"),
            ):
                with self.assertRaisesRegex(firmware_manifest.ManifestError, "download failed"):
                    sync_firmware.download_asset(asset, destination, maximum_size=1024)
            self.assertEqual(b"known-good", destination.read_bytes())
            self.assertEqual([], list(Path(temporary).glob(".*")))

    def test_alias_selection_follows_api_order_not_tag_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            newest = vault / "archive" / "beta" / "beta-0.1.10"
            older = vault / "archive" / "beta" / "beta-0.1.9"
            newest.mkdir(parents=True)
            older.mkdir(parents=True)
            releases = [
                {"tag_name": "beta-0.1.10", "prerelease": True},
                {"tag_name": "beta-0.1.9", "prerelease": True},
            ]

            def staged(release, channel, api_ok):
                del channel, api_ok
                directory = newest if release["tag_name"] == "beta-0.1.10" else older
                return directory, {"release": release["tag_name"]}

            def metadata(release, directory, manifest):
                del directory, manifest
                return {"tag_name": release["tag_name"]}

            with (
                mock.patch.object(sync_firmware, "VAULT", vault),
                mock.patch.object(sync_firmware, "STATE_FILE", vault / "state.json"),
                mock.patch.object(sync_firmware, "github_request", return_value=releases),
                mock.patch.object(sync_firmware, "stage_release", side_effect=staged),
                mock.patch.object(sync_firmware, "_release_metadata", side_effect=metadata),
                mock.patch.object(sync_firmware, "_replace_symlink") as replace_symlink,
                mock.patch.object(sync_firmware, "_atomic_json"),
            ):
                sync_firmware.sync_releases()

            self.assertEqual(
                (vault / "dev", newest),
                replace_symlink.call_args_list[0].args,
            )

    def test_unsigned_release_never_becomes_a_channel_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            unsigned_release = {
                "tag_name": "beta-unsigned",
                "prerelease": True,
                "published_at": "2026-08-11T00:00:00Z",
                "assets": [],
            }
            with (
                mock.patch.object(sync_firmware, "VAULT", vault),
                mock.patch.object(sync_firmware, "STATE_FILE", vault / "state.json"),
                mock.patch.object(sync_firmware, "github_request", return_value=[unsigned_release]),
            ):
                sync_firmware.sync_releases()
            self.assertFalse((vault / "dev").exists())
            self.assertFalse((vault / "latest").exists())
            self.assertEqual([], json.loads((vault / "releases.json").read_text()))


if __name__ == "__main__":
    unittest.main()
