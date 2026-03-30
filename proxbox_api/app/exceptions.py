"""Application-wide exception handlers."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from proxbox_api.exception import ProxboxException


def register_exception_handlers(app: FastAPI) -> None:
    """Register ProxboxException and generic exception JSON handlers."""

    @app.exception_handler(ProxboxException)
    async def proxbox_exception_handler(request: Request, exc: ProxboxException) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "message": exc.message,
                "detail": exc.detail,
                "python_exception": exc.python_exception,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "message": "Internal server error",
                "detail": str(exc),
                "python_exception": str(exc),
            },
        )
