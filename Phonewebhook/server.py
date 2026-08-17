"""Apollo phone-reveal webhook.

Run locally, then expose HTTPS (required by Apollo):

    python -m Phonewebhook
    python -m Phonewebhook --tunnel

Set APOLLO_WEBHOOK_URL to the printed public URL before using --reveal-phone.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from src.apollo import phones_from_webhook
from src.config import Settings
from src.db import Database

logger = logging.getLogger(__name__)

app = FastAPI(title="Apollo phone webhook", docs_url=None, redoc_url=None)
_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database(Settings.load(require_apollo=False))
    return _db


def _expected_secret() -> str:
    return os.getenv("PHONE_WEBHOOK_SECRET", "").strip()


def _check_secret(token: str | None, request: Request) -> None:
    expected = _expected_secret()
    if not expected:
        return
    header = request.headers.get("x-webhook-secret", "")
    if token == expected or header == expected:
        return
    raise HTTPException(status_code=401, detail="Invalid webhook secret")


@app.get("/")
def root() -> dict[str, str]:
    return {"status": "ok", "health": "/health", "webhook": "/apollo/phone"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/apollo/phone")
async def apollo_phone(
    request: Request,
    token: str | None = Query(default=None),
) -> JSONResponse:
    _check_secret(token, request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        payload = {"people": payload} if isinstance(payload, list) else {}
    updated = _apply_phones(payload)
    return JSONResponse({"status": "ok", "updated": updated})


def _apply_phones(payload: dict[str, Any]) -> int:
    phones = phones_from_webhook(payload)
    if not phones:
        logger.info("Apollo phone webhook had no numbers")
        return 0
    updated = get_db().update_phones(phones)
    logger.info("Updated %s contact phone numbers from Apollo webhook", updated)
    return updated


def _webhook_path() -> str:
    secret = _expected_secret()
    path = "/apollo/phone"
    if secret:
        path = f"{path}?token={secret}"
    return path


def _print_urls(host: str, port: int, public_url: str | None = None) -> None:
    path = _webhook_path()
    local = f"http://{host}:{port}{path}"
    logger.info("Listening on %s", local)
    if public_url:
        public = public_url.rstrip("/") + path
        logger.info("Give Apollo this URL (set APOLLO_WEBHOOK_URL): %s", public)
    else:
        logger.info(
            "Apollo needs public HTTPS. Re-run with --tunnel, or put a reverse proxy "
            "in front and set APOLLO_WEBHOOK_URL to https://YOUR_HOST%s",
            path,
        )


def _start_cloudflare_tunnel(port: int) -> str:
    binary = shutil.which("cloudflared")
    if not binary:
        raise SystemExit(
            "cloudflared is not installed. Install it from "
            "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/ "
            "or run: winget install Cloudflare.cloudflared"
        )
    proc = subprocess.Popen(
        [binary, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    url_pattern = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        match = url_pattern.search(line)
        if match:
            threading.Thread(target=_drain, args=(proc,), daemon=True).start()
            return match.group(0)
    raise SystemExit("cloudflared started but no public URL was printed")


def _drain(proc: subprocess.Popen[str]) -> None:
    if proc.stdout is None:
        return
    for line in proc.stdout:
        sys.stdout.write(line)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Receive Apollo phone enrichment webhooks.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.getenv("PHONE_WEBHOOK_PORT", "8787")))
    parser.add_argument(
        "--tunnel",
        action="store_true",
        help="Open a temporary public HTTPS URL with cloudflared (trycloudflare).",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    import time

    import uvicorn

    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
        if not thread.is_alive():
            raise SystemExit("Webhook server failed to start")

    public_url = _start_cloudflare_tunnel(args.port) if args.tunnel else None
    _print_urls(args.host, args.port, public_url)
    thread.join()
