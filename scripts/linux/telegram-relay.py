#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
RELAY_SECRET = os.environ.get("TELEGRAM_RELAY_SECRET", "")
LISTEN_HOST = os.environ.get("TELEGRAM_RELAY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("TELEGRAM_RELAY_PORT", "8787"))
TIMEOUT_SECONDS = float(os.environ.get("TELEGRAM_RELAY_TIMEOUT_SECONDS", "20"))
ALLOWED_METHODS = {"getMe", "getUpdates", "sendMessage"}


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "HfdTelegramRelay/0.1"

    def do_GET(self) -> None:  # noqa: N802
        method = self._telegram_method()
        if method is None:
            return
        if method not in {"getMe", "getUpdates"}:
            self._send_json(405, {"ok": False, "description": "method not allowed"})
            return
        query = urllib.parse.urlsplit(self.path).query
        self._proxy(method, query=query)

    def do_POST(self) -> None:  # noqa: N802
        method = self._telegram_method()
        if method is None:
            return
        if method != "sendMessage":
            self._send_json(405, {"ok": False, "description": "method not allowed"})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b"{}"
        self._proxy(method, body=body)

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _telegram_method(self) -> str | None:
        if not BOT_TOKEN:
            self._send_json(500, {"ok": False, "description": "bot token is not configured"})
            return None
        if not RELAY_SECRET:
            self._send_json(500, {"ok": False, "description": "relay secret is not configured"})
            return None
        if self.headers.get("X-HFD-Relay-Secret") != RELAY_SECRET:
            self._send_json(401, {"ok": False, "description": "unauthorized"})
            return None
        path = urllib.parse.urlsplit(self.path).path
        prefix = "/telegram/"
        if not path.startswith(prefix):
            self._send_json(404, {"ok": False, "description": "not found"})
            return None
        method = path[len(prefix) :]
        if method not in ALLOWED_METHODS:
            self._send_json(404, {"ok": False, "description": "unsupported method"})
            return None
        return method

    def _proxy(self, method: str, query: str = "", body: bytes | None = None) -> None:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
        if query:
            url = f"{url}?{query}"
        headers = {"Content-Type": "application/json"}
        request = urllib.request.Request(url, data=body, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                payload = response.read()
                status = response.status
                content_type = response.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            status = exc.code
            content_type = exc.headers.get("Content-Type", "application/json")
        except Exception as exc:  # noqa: BLE001
            self._send_json(502, {"ok": False, "description": exc.__class__.__name__})
            return
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), RelayHandler)
    print(f"HFD Telegram relay listening on {LISTEN_HOST}:{LISTEN_PORT}")
    server.serve_forever()
