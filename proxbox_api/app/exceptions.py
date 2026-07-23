"""Application-wide exception handlers."""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.runtime_settings import get_bool

_SAFE_VALIDATION_LOCATION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


def _safe_validation_location(value: object) -> int | str:
    """Keep structural locations without reflecting attacker-controlled keys."""

    if isinstance(value, int):
        return value
    text = str(value)
    return text if _SAFE_VALIDATION_LOCATION_RE.fullmatch(text) else "[REDACTED_FIELD]"


def _safe_validation_locations(error: dict[str, object]) -> list[int | str]:
    """Sanitize schema locations, including valid-looking arbitrary extra keys."""

    raw_location = error.get("loc")
    location = list(raw_location) if isinstance(raw_location, list | tuple) else []
    extra_index = len(location) - 1 if error.get("type") == "extra_forbidden" else -1
    return [
        "[REDACTED_FIELD]" if index == extra_index else _safe_validation_location(item)
        for index, item in enumerate(location)
    ]


def _expose_internal_errors(app: FastAPI) -> bool:
    return get_bool(
        settings_key="expose_internal_errors",
        env="PROXBOX_EXPOSE_INTERNAL_ERRORS",
        default=False,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register ProxboxException and generic exception JSON handlers."""

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        if not request.url.path.startswith("/ceph/v2"):
            return await request_validation_exception_handler(request, exc)
        # FastAPI's default payload includes the rejected ``input`` value. Ceph
        # operations deliberately reject arbitrary secret aliases, so echoing
        # that input would turn validation into a credential disclosure path.
        errors = [
            {
                "type": str(error.get("type") or "value_error"),
                "loc": _safe_validation_locations(error),
                "msg": "Request validation failed.",
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(ProxboxException)
    async def proxbox_exception_handler(request: Request, exc: ProxboxException) -> JSONResponse:
        expose = _expose_internal_errors(request.app)
        return JSONResponse(
            status_code=exc.http_status_code,
            content={
                "message": exc.message,
                "detail": exc.detail,
                "python_exception": exc.python_exception if expose else None,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        if request.url.path.startswith("/ceph/v2"):
            correlation_id = uuid4().hex
            logger.error(
                "Unhandled Ceph v2 request failure correlation_id=%s",
                correlation_id,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "message": "Internal server error",
                    "detail": "The Ceph request could not be completed safely.",
                    "python_exception": None,
                    "correlation_id": correlation_id,
                },
            )
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        if _expose_internal_errors(request.app):
            return JSONResponse(
                status_code=500,
                content={
                    "message": "Internal server error",
                    "detail": str(exc),
                    "python_exception": str(exc),
                },
            )
        return JSONResponse(
            status_code=500,
            content={
                "message": "Internal server error",
                "detail": "An unexpected error occurred.",
                "python_exception": None,
            },
        )
