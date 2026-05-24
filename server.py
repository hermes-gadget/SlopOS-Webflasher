#!/usr/bin/env python3
"""SlopOS Web Flasher Server - serves static files + proxies firmware downloads."""

import http.server
import json
import os
import sys
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8082

GITHUB_API = "https://api.github.com/repos/hermes-gadget/SlopOS-tdeck/releases/assets"

class CORSHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Accept, Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/proxy/firmware/"):
            # /api/proxy/firmware/{asset_id}
            asset_id = self.path.split("/api/proxy/firmware/")[1].strip("/")
            if not asset_id.isdigit():
                self.send_error(400, "Invalid asset ID")
                return
            url = f"{GITHUB_API}/{asset_id}"
            req = urllib.request.Request(url, headers={
                "Accept": "application/octet-stream",
                "User-Agent": "SlopOSWebFlasher/1.0",
            })
            try:
                with urllib.request.urlopen(req) as resp:
                    data = resp.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Content-Disposition", 'attachment; filename="firmware-merged.bin"')
                    self.end_headers()
                    self.wfile.write(data)
            except Exception as e:
                self.send_error(502, f"Proxy error: {e}")
            return
        return super().do_GET()

    def log_message(self, fmt, *args):
        print(f"[flasher] {args[0]} {args[1]} {args[2]}")

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print(f"Serving on http://0.0.0.0:{PORT} (static + firmware proxy)")
    http.server.HTTPServer(("0.0.0.0", PORT), CORSHandler).serve_forever()
