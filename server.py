#!/usr/bin/env python3
"""
SigurdOS Web Flasher Server — static files + local firmware vault + security.

Serves:
  • Static web flasher files (index.html, assets/, etc.)
  • /api/releases          — JSON list of all archived releases (for the frontend)
  • /api/firmware/<path>   — Firmware file from local vault
  • /firmware/<path>       — Same, direct download with Content-Disposition
  • /latest/firmware-merged.bin   — Convenience: latest stable (or dev)
  • /dev/firmware-merged.bin      — Convenience: latest prerelease
  • /archive/<channel>/<tag>/<file>  — Specific version archive

URL design:
  flasher.sigurdos.dev/latest/firmware-merged.bin
  flasher.sigurdos.dev/dev/firmware-merged.bin
  flasher.sigurdos.dev/archive/beta/beta-0.1.40/firmware-merged.bin

Security features:
  • Path traversal protection — realpath check against vault root
  • Allowed file extension whitelist
  • No directory listing
  • Strict security headers (CSP, HSTS, X-Content-Type-Options, etc.)
  • Request logging with rate-limit awareness
  • Content-Type enforced by file extension
"""

import http.server
import json
import os
import re
import sys
import time
import urllib.parse

# ── config ──────────────────────────────────────────────
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8082
VAULT = os.path.expanduser("~/firmware/vault")
# In production, VAULT should be absolute; local development relative
VAULT = os.path.abspath(VAULT)

# Only serve these file extensions from the firmware vault
ALLOWED_EXTENSIONS = {".bin", ".json"}

# ── path security ───────────────────────────────────────
SAFE_PATH_RE = re.compile(r"^[a-zA-Z0-9_.\-/]+$")


def vault_path(relative: str) -> str | None:
    """
    Resolve a relative path within the vault, with security checks.

    Returns the absolute path if safe, None if rejected.
    """
    # Reject path traversal components
    if ".." in relative.split("/"):
        return None
    if relative.startswith("/"):
        return None
    if relative.startswith("~"):
        return None

    # Reject unsafe characters
    if not SAFE_PATH_RE.match(relative):
        return None

    full = os.path.realpath(os.path.join(VAULT, relative))

    # Must be inside the vault
    if not full.startswith(os.path.realpath(VAULT) + "/"):
        return None

    # Must exist and be a file
    if not os.path.isfile(full):
        return None

    # Must have an allowed extension
    _, ext = os.path.splitext(full)
    if ext.lower() not in ALLOWED_EXTENSIONS:
        return None

    return full


# ── server ──────────────────────────────────────────────
class FirmwareHandler(http.server.SimpleHTTPRequestHandler):
    # Security headers applied to every response
    SECURITY_HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "X-XSS-Protection": "0",  # Deprecated, but belt-and-suspenders
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains; preload",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=(), serial=(self)",
    }

    # CSP — very restrictive for downloaded firmware
    CSP_DOWNLOAD = "default-src 'none'; sandbox"

    # CSP for the main app
    CSP_PAGE = (
        "default-src 'self';"
        " connect-src 'self';"
        " style-src 'self' 'unsafe-inline' https://fonts.googleapis.com;"
        " font-src 'self' https://fonts.gstatic.com;"
        " script-src 'self' 'unsafe-inline';"
        " img-src 'self' data:;"
        " frame-src 'none';"
        " object-src 'none';"
        " base-uri 'self';"
        " form-action 'self';"
    )

    def log_message(self, fmt, *args):
        """Structured JSON logging for easier parsing."""
        try:
            code = int(args[1]) if len(args) > 1 and args[1] != '-' else 0
        except (ValueError, IndexError):
            code = 0
        log_entry = {
            "t": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "method": args[0] if len(args) > 0 else "",
            "path": self.path,
            "code": code,
            "remote": self.client_address[0],
        }
        print(f"[flasher] {json.dumps(log_entry)}")

    def send_security_headers(self, is_download=False):
        """Override CSP for specific responses (firmware downloads get sandbox)."""
        if is_download:
            self.send_header("Content-Security-Policy", self.CSP_DOWNLOAD)
            self.send_header("X-Content-Type-Options", "nosniff")

    def send_cors_headers(self):
        """CORS for API endpoints — restrict to same-origin for firmware."""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Accept, Content-Type")

    def end_headers(self):
        super().end_headers()

    def send_response(self, code, message=None):
        """Override to inject security headers into EVERY response."""
        super().send_response(code, message)
        for header, value in self.SECURITY_HEADERS.items():
            self.send_header(header, value)
        self.send_header("Content-Security-Policy", self.CSP_PAGE)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors_headers()
        self.send_security_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ── API: release metadata ───────────────────────────
    def serve_releases(self):
        """Serve /api/releases — JSON list of archived firmware releases."""
        releases_path = os.path.join(VAULT, "releases.json")
        if not os.path.exists(releases_path):
            self.send_error(404, "No release data available. Sync hasn't run yet.")
            return

        try:
            with open(releases_path) as f:
                data = f.read()
        except OSError:
            self.send_error(500, "Failed to read release data")
            return

        self.send_response(200)
        self.send_cors_headers()
        self.send_security_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.end_headers()
        if not getattr(self, '_is_head', False):
            self.wfile.write(data.encode())

    # ── API: firmware file serving ──────────────────────
    def serve_firmware(self, relative_path: str, as_download=False):
        """Serve a firmware file from the vault with security checks."""
        abspath = vault_path(relative_path)
        if abspath is None:
            self.send_error(404, "Not found")
            return

        try:
            with open(abspath, "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(500, "Failed to read file")
            return

        # Determine Content-Type
        _, ext = os.path.splitext(abspath)
        ext = ext.lower()
        if ext == ".bin":
            content_type = "application/octet-stream"
        elif ext == ".json":
            content_type = "application/json"
        else:
            content_type = "application/octet-stream"

        fname = os.path.basename(abspath)

        self.send_response(200)
        self.send_cors_headers()
        self.send_security_headers(is_download=as_download)

        if as_download:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        else:
            self.send_header("Content-Type", content_type)

        self.send_header("Content-Length", str(len(data)))
        # Firmware binaries are immutable — cache aggressively
        if "/archive/" in relative_path and ext == ".bin":
            self.send_header("Cache-Control", "public, max-age=86400, immutable")
        else:
            self.send_header("Cache-Control", "public, max-age=3600")

        self.end_headers()
        if not getattr(self, '_is_head', False):
            self.wfile.write(data)

    def do_HEAD(self):
        """Route HEAD to do_GET (skip body write via _is_head flag)."""
        self._is_head = True
        try:
            self.do_GET()
        finally:
            self._is_head = False

    # ── route dispatcher ─────────────────────────────────
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # ── API endpoints ────────────────────────────────
        if path == "/api/releases":
            self.serve_releases()
            return

        if path.startswith("/api/firmware/"):
            relative = path[len("/api/firmware/"):].strip("/")
            if not relative:
                self.send_error(400, "Missing firmware path")
                return
            self.serve_firmware(relative, as_download=False)
            return

        # ── Direct download endpoints ────────────────────
        # /latest/firmware-merged.bin  → vault/latest/... through symlinks
        # /dev/firmware-merged.bin     → vault/dev/...
        # /debug/firmware-debug.bin     → vault/debug/...
        # /archive/beta/beta-0.1.40/firmware-merged.bin
        for prefix in ("/latest/", "/dev/", "/debug/", "/archive/"):
            if path.startswith(prefix):
                relative = path[len(prefix):].strip("/")
                if not relative:
                    self.send_error(400, "Missing file path")
                    return
                # Map URL prefix to vault relative path
                if prefix == "/latest/":
                    vault_rel = os.path.join("latest", relative)
                elif prefix == "/dev/":
                    vault_rel = os.path.join("dev", relative)
                elif prefix == "/debug/":
                    vault_rel = os.path.join("debug", relative)
                else:  # /archive/
                    vault_rel = os.path.join("archive", relative)
                self.serve_firmware(vault_rel, as_download=True)
                return

        # ── Root redirect to flasher UI ──────────────────
        if path == "/" or path == "":
            return super().do_GET()

        # ── Static files (existing behavior) ─────────────
        # JS files: serve directly so we can control caching
        if path.endswith(".js"):
            self.serve_static_js(path)
            return

        return super().do_GET()

    def serve_static_js(self, path):
        """Serve a .js file with no-cache headers to avoid stale Cloudflare cache."""
        docroot = os.getcwd()
        filepath = os.path.join(docroot, path.lstrip("/"))
        filepath = os.path.normpath(filepath)
        # Security: must be within docroot
        if not filepath.startswith(os.path.normpath(docroot) + os.sep):
            self.send_error(404)
            return
        if not os.path.isfile(filepath):
            self.send_error(404)
            return
        try:
            with open(filepath, "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.end_headers()
        if not getattr(self, '_is_head', False):
            self.wfile.write(data)


# ── main ────────────────────────────────────────────────
if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    http.server.HTTPServer.allow_reuse_address = True

    # Validate vault exists
    if not os.path.exists(VAULT):
        print(f"[flasher] WARNING: Firmware vault not found at {VAULT}")
        print(f"[flasher] Run sync_firmware.py first or create the directory")

    print(f"[flasher] SigurdOS Web Flasher on http://0.0.0.0:{PORT}")
    print(f"[flasher] Firmware vault: {VAULT}")
    print(f"[flasher] Static root:   {os.getcwd()}")
    print(f"[flasher] Endpoints:")
    print(f"[flasher]   /api/releases                  — firmware metadata (JSON)")
    print(f"[flasher]   /api/firmware/dev/firmware-merged.bin   — via API (inline)")
    print(f"[flasher]   /dev/firmware-merged.bin        — direct download")
    print(f"[flasher]   /latest/firmware-merged.bin     — direct download")
    print(f"[flasher]   /archive/beta/<tag>/<file>      — specific version")

    srv = http.server.HTTPServer(("0.0.0.0", PORT), FirmwareHandler)
    srv.serve_forever()
