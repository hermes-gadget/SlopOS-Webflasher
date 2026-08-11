import http.server
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import server


class StaticServingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        (root / "assets/vendor").mkdir(parents=True)
        (root / ".git").mkdir()
        (root / "vault/dev").mkdir(parents=True)
        for relative, content in {
            "index.html": b"safe index",
            "assets/app.js": b"safe app",
            "assets/firmware-security.js": b"safe security",
            "assets/md5.js": b"safe md5",
            "assets/serial-cleanup.js": b"safe cleanup",
            "assets/styles.css": b"safe css",
            "assets/sigurdos-banner.png": b"safe png",
            "assets/vendor/esptool-js-bundle.js": b"safe vendor",
            ".env": b"SECRET=must-not-leak",
            ".git/config": b"private git metadata",
            "server.py": b"source must not leak",
            "vault/dev/firmware-manifest.json": b"{}\n",
            "vault/dev/firmware-manifest.sig": b"c2lnbmF0dXJl\n",
        }.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.static_patch = mock.patch.object(server, "STATIC_ROOT", str(root))
        self.vault_patch = mock.patch.object(server, "VAULT", str(root / "vault"))
        self.static_patch.start()
        self.vault_patch.start()
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), server.FirmwareHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.static_patch.stop()
        self.vault_patch.stop()
        self.temporary.cleanup()

    def status(self, path):
        try:
            with urllib.request.urlopen(self.base_url + path, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_exact_public_files_are_served(self):
        self.assertEqual((200, b"safe index"), self.status("/"))
        self.assertEqual((200, b"safe app"), self.status("/assets/app.js"))
        self.assertEqual((200, b"safe md5"), self.status("/assets/md5.js"))
        self.assertEqual((200, b"safe cleanup"), self.status("/assets/serial-cleanup.js"))

    def test_checkout_secrets_source_and_directories_are_denied(self):
        for path in ("/.env", "/.git/config", "/server.py", "/assets/"):
            with self.subTest(path=path):
                status, body = self.status(path)
                self.assertEqual(404, status)
                self.assertNotIn(b"must-not-leak", body)
                self.assertNotIn(b"private git metadata", body)
                self.assertNotIn(b"source must not leak", body)

    def test_signed_manifest_and_signature_are_served_from_vault(self):
        self.assertEqual((200, b"{}\n"), self.status("/api/firmware/dev/firmware-manifest.json"))
        self.assertEqual(
            (200, b"c2lnbmF0dXJl\n"),
            self.status("/api/firmware/dev/firmware-manifest.sig"),
        )

    def test_server_defaults_to_loopback(self):
        self.assertEqual("127.0.0.1", server.DEFAULT_HOST)


if __name__ == "__main__":
    unittest.main()
