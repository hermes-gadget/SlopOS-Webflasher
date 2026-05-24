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

        if self.path.startswith("/api/proxy/tile?"):
            # /api/proxy/tile?z={z}&x={x}&y={y} — proxies OSM tiles with CORS
            import urllib.parse
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                z = params["z"][0]
                x = params["x"][0]
                y = params["y"][0]
            except (KeyError, IndexError):
                self.send_error(400, "Missing z, x, y params")
                return
            tile_url = f"https://tile.openstreetmap.org/{z}/{x}/{y}.png"
            req = urllib.request.Request(tile_url, headers={
                "User-Agent": "SlopOSTileProxy/1.0",
            })
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = resp.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "public, max-age=86400")
                    # CORS headers so browser JS can read response
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
            except Exception as e:
                self.send_error(502, f"Tile proxy error: {e}")
            return

        return super().do_GET()

    def log_message(self, fmt, *args):
        print(f"[flasher] {args[0]} {args[1]} {args[2]}")

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print(f"Serving on http://0.0.0.0:{PORT} (static + firmware proxy)")
    http.server.HTTPServer.allow_reuse_address = True
    srv = http.server.HTTPServer(("0.0.0.0", PORT), CORSHandler)
    srv.serve_forever()
