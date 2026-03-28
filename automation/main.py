"""ASGI compatibility shim for legacy `automation.main:app` entrypoints."""

from proxbox_api.main import app

__all__ = ["app"]
