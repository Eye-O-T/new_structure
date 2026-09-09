"""운영 화면만 제공한다. 데이터 조회·변경 권한은 기존 API가 검사한다."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(include_in_schema=False)
_ASSETS = Path(__file__).resolve().parents[1] / "web"
_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
}


# 고정 HTML만 제공하며 운영 자료의 접근 권한은 화면이 호출하는 API가 검사한다.
@router.get("/admin")
@router.get("/admin/")
async def admin_page() -> FileResponse:
    return FileResponse(_ASSETS / "index.html", headers=_HEADERS)


# 허용된 동일 출처 스크립트를 보안 헤더와 함께 제공한다.
@router.get("/admin/admin.js")
async def admin_script() -> FileResponse:
    return FileResponse(
        _ASSETS / "admin.js", media_type="text/javascript", headers=_HEADERS
    )


# 화면과 같은 캐시·보안 정책으로 정적 스타일 파일을 전달한다.
@router.get("/admin/admin.css")
async def admin_style() -> FileResponse:
    return FileResponse(_ASSETS / "admin.css", headers=_HEADERS)
