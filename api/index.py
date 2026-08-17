"""Vercel Python entrypoint (api/index.py). Exports the FastAPI app."""

from Phonewebhook.server import app

__all__ = ["app"]
