from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Phonewebhook.server import _apply_phones, _expected_secret


def _send(handler: BaseHTTPRequestHandler, status: int, body: dict) -> None:
    payload = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _authorized(handler: BaseHTTPRequestHandler) -> bool:
    expected = _expected_secret()
    if not expected:
        return True
    query = parse_qs(urlparse(handler.path).query)
    token = (query.get("token") or [None])[0]
    header = handler.headers.get("x-webhook-secret", "")
    return token == expected or header == expected


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if not _authorized(self):
            _send(self, 401, {"error": "Invalid webhook secret"})
            return
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            _send(self, 400, {"error": "Invalid JSON"})
            return
        if not isinstance(payload, dict):
            payload = {"people": payload} if isinstance(payload, list) else {}
        try:
            updated = _apply_phones(payload)
        except Exception as exc:
            _send(self, 500, {"error": str(exc)})
            return
        _send(self, 200, {"status": "ok", "updated": updated})

    def log_message(self, format: str, *args: object) -> None:
        return
