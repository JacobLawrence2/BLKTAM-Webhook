"""Vercel FastAPI entrypoint. Do not rename: Vercel looks for app.py."""

from Phonewebhook.server import app

__all__ = ["app"]
