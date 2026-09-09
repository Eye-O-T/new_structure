# 입력·DB·서버 오류를 일관된 HTTP 상태 코드와 JSON 오류 응답으로 바꾼다.

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


# HTTP 상태와 기계 판독 코드·세부 정보를 함께 전달하는 내부 API 예외이다.
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


# details가 없을 때도 객체를 유지하여 오류 응답의 구조가 변하지 않게 한다.
def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": {} if details is None else details,
        }
    }


# 예상 오류와 내부 오류를 공통 JSON 형식으로 변환하는 처리기를 앱에 등록한다.
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
        # 요청에 해시·토큰이 있을 수 있어 오류 응답에는 입력값을 넣지 않는다.
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

    # SQL 원문·제약 이름을 공개하지 않고 중복 또는 참조 충돌을 409로 전달한다.
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

    # 예외 내용은 서버 로그에 남기고 응답에는 일반화된 오류만 제공한다.
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


def _not_found(resource: str) -> ApiError:
    return ApiError(
        404, f"{resource.upper()}_NOT_FOUND", "요청한 항목을 찾을 수 없습니다."
    )
