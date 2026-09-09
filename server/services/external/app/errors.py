# 다른 서비스의 실패를 공개 API 오류로 바꾸되 토큰이나 비밀값이 응답에 노출되지 않게 한다.

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .clients.data import DataServiceError
from .clients.edge import EdgeControlError
from .clients.mediamtx import MediaControlError


# 허용한 카메라 충돌 정보만 공개하고 나머지 Data 오류의 내부 메시지는 숨긴다.
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


# 설정 예외의 값이나 비밀 경로를 노출하지 않고 서비스 준비 실패로 알린다.
async def handle_configuration_error(_: Request, __: RuntimeError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": "Service configuration is unavailable"},
    )


# 라우터가 공통 서비스 예외를 발생시키면 동일한 응답 변환을 거치도록 등록한다.
def register_exception_handlers(application: FastAPI) -> None:
    application.add_exception_handler(DataServiceError, handle_data_error)
    application.add_exception_handler(EdgeControlError, handle_edge_error)
    application.add_exception_handler(MediaControlError, handle_media_error)
    application.add_exception_handler(RequestValidationError, handle_validation_error)
    application.add_exception_handler(RuntimeError, handle_configuration_error)
