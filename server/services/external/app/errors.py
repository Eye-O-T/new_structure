# 다른 서비스의 실패를 공개 API 오류로 바꾸되 토큰이나 비밀값이 응답에 노출되지 않게 한다.

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .clients.data import DataServiceError
from .clients.edge import EdgeControlError
from .clients.mediamtx import MediaControlError


async def handle_data_error(_: Request, exc: DataServiceError) -> JSONResponse:
    if exc.code in {"CAMERA_HAS_HISTORY", "CAMERA_LIMIT_REACHED"}:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": {},
                }
            },
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": "Data service request failed"},
    )


async def handle_edge_error(_: Request, exc: EdgeControlError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        },
    )


async def handle_media_error(_: Request, exc: MediaControlError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": {},
            }
        },
    )


async def handle_validation_error(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    safe_errors = []
    for error in exc.errors():
        safe_errors.append(
            {
                "type": error.get("type", "validation_error"),
                "loc": error.get("loc", ()),
                "msg": error.get("msg", "Invalid request"),
            }
        )
    return JSONResponse(status_code=422, content={"detail": safe_errors})


async def handle_configuration_error(_: Request, __: RuntimeError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": "Service configuration is unavailable"},
    )


def register_exception_handlers(application: FastAPI) -> None:
    application.add_exception_handler(DataServiceError, handle_data_error)
    application.add_exception_handler(EdgeControlError, handle_edge_error)
    application.add_exception_handler(MediaControlError, handle_media_error)
    application.add_exception_handler(RequestValidationError, handle_validation_error)
    application.add_exception_handler(RuntimeError, handle_configuration_error)
