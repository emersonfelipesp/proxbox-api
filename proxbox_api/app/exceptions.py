"""Application-wide exception handlers."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.runtime_settings import get_bool


def _expose_internal_errors(app: FastAPI) -> bool:
    return get_bool(
        settings_key="expose_internal_errors",
        env="PROXBOX_EXPOSE_INTERNAL_ERRORS",
        default=False,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register secret-safe validation, ProxboxException, and generic handlers."""

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        """Return a stable error without reflecting Pydantic input or context.

        ``RequestValidationError.errors()`` can contain the complete request body in
        its ``input`` member. Cloud image requests can contain keys, user-data, and
        signed URLs, so neither the exception nor its error details cross this
        application boundary.
        """

        del request, exc
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {
                        "type": "request_validation_error",
                        "loc": ["body"],
                        "msg": "Request validation failed.",
                    }
                ]
            },
        )

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
