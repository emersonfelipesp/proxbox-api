"""Application composition: factory, middleware, and cross-cutting HTTP setup."""

from proxbox_api.app.factory import create_app

__all__ = ("create_app",)
