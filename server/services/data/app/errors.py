"""Stable JSON error contract."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": {} if details is None else details,
        }
    }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Do not include input values: request bodies can contain hashes or tokens.
        details = [
            {
                "location": list(error["loc"]),
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_body(
                "VALIDATION_ERROR", "요청 값이 유효하지 않습니다.", details
            ),
        )

    @app.exception_handler(HTTPException)
    async def handle_http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        message = str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body("HTTP_ERROR", message),
            headers=exc.headers,
        )

    @app.exception_handler(sqlite3.IntegrityError)
    async def handle_integrity_error(
        _request: Request, _exc: sqlite3.IntegrityError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=error_body(
                "RESOURCE_CONFLICT",
                "중복 값 또는 참조 관계로 요청을 처리할 수 없습니다.",
            ),
        )

    @app.exception_handler(ValueError)
    async def handle_value_error(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_body("VALIDATION_ERROR", str(exc)),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception("Unhandled Data Service error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=error_body(
                "INTERNAL_ERROR", "요청을 처리하는 중 내부 오류가 발생했습니다."
            ),
        )
