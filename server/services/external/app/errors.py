# 개발 환경에서는 하위 서비스의 원래 오류를 호출자에게 전달한다.

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .clients.data import DataServiceError
from .clients.edge import EdgeControlError
from .clients.mediamtx import MediaControlError


# Data 서비스가 반환한 오류 코드·메시지·세부 정보를 그대로 전달한다.
async def handle_data_error(_: Request, exc: DataServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": getattr(exc, "details", {}),
            }
        },
    )


# Edge 제어 계층이 정규화한 상태 코드·이유·세부 정보를 공개 오류 형식으로 전달한다.
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


# 송출 해제 실패를 API 오류로 바꾸어 호출자가 비활성 상태에서 재시도할 수 있게 한다.
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


# 입력 원문과 예외 컨텍스트를 제외하고 필드 위치·검증 종류·메시지만 반환한다.
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


# 개발 중에는 설정 예외의 원문을 확인할 수 있게 한다.
async def handle_configuration_error(_: Request, exc: RuntimeError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": str(exc)},
    )


# 라우터가 공통 서비스 예외를 발생시키면 동일한 응답 변환을 거치도록 등록한다.
def register_exception_handlers(application: FastAPI) -> None:
    application.add_exception_handler(DataServiceError, handle_data_error)
    application.add_exception_handler(EdgeControlError, handle_edge_error)
    application.add_exception_handler(MediaControlError, handle_media_error)
    application.add_exception_handler(RequestValidationError, handle_validation_error)
    application.add_exception_handler(RuntimeError, handle_configuration_error)
