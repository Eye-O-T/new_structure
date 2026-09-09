"""공개 API 문서를 생성한다. 서버 시작·DB 연결·운영 비밀값은 필요하지 않다."""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
import sys
from typing import Any, Literal

# 파일 경로로 실행해도 저장소와 공유 계약 모듈을 찾을 수 있게 한다.
ROOT = Path(__file__).resolve().parents[4]
for directory in (ROOT, ROOT / "lib"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import yaml  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from ai_cctv_core.contracts.objects import LiveObject, LiveObjects  # noqa: E402
from server.services.external.app.main import create_app  # noqa: E402
from server.services.external.app.security.permissions import require_admin  # noqa: E402


DESCRIPTION = """모바일 등 외부 클라이언트를 교체할 때 유지해야 할 공개 HTTP API이다.
앱은 중앙 서버의 HTTPS 주소(도메인과 포트)에 접속한다. API 경로는 /api/v1로 시작하고,
실시간·중앙 녹화 영상은 /hls·/playback을 사용한다. Data·Edge·MediaMTX의 내부 포트에는
직접 연결하지 않는다. 이 파일을 OpenAPI 3.1을 지원하는 뷰어에서 열면 요청·응답 형식을 볼 수 있다.

### 로그인과 권한
1. POST /api/v1/auth/login으로 access_token·refresh_token과 user를 받는다.
2. API에는 Authorization: Bearer <access_token> 또는 Access Cookie를 보낸다.
   두 가지가 함께 있으면 Bearer가 우선한다. admin은 전체, viewer는 허용 카메라만 조회한다.
3. 401이면 현재 refresh_token으로 한 번 갱신하고 원래 요청을 재시도한다.
   갱신은 새 access·refresh 쌍을 반환하며 이전 refresh는 재사용할 수 없다.
   앱은 동시 갱신을 하나로 합치고 최신 두 토큰을 함께 저장한다. 갱신 실패는 재로그인한다.
4. 로그아웃에는 현재 access와 refresh를 함께 보낸다. 서버는 제출된 두 토큰을 폐기하고
   refresh 계열의 기기 등록·대기 알림과 쿠키를 삭제한다. 다른 access까지 전부 즉시 철회하는
   전체 기기 로그아웃 API는 아니다. 이미 회전된 refresh 대신 반드시 최신값을 보낸다.
기본 쿠키명은 ai_cctv_access, ai_cctv_refresh이며 배포 설정으로 바뀔 수 있다.
로그인·갱신은 HttpOnly 쿠키도 설정한다. 기본 Secure=true, SameSite=lax,
access Path=/, refresh Path=/api/v1/auth, 기본 수명은 각각 900초·604800초이다.
실제 access 수명은 expires_in을 따른다. 토큰을 URL·로그에 기록하지 않는다.

### 영상 재생: Nginx 경로
GET /api/v1/cameras/{camera_id}/live의 url 또는
GET /api/v1/recordings/{segment_id}/playback의 playback_url을 그대로 사용한다.
PUBLIC_BASE_URL 설정 시 HTTPS 절대 URL, 없으면 중앙 서버 주소 기준 상대 URL이다.
라이브 기본 주소는 /hls/{camera_id}/index.m3u8이고 재생목록과 영상 조각을 받는 HLS이다.
중앙 녹화 기본 주소는 /playback/get?path={camera_id}&start={UTC}&duration={초}&format=fmp4다.
이 두 미디어 경로는 Nginx가 MediaMTX에 중계하며 FastAPI paths에 포함되지 않는다.
auth.method=cookie는 기본 플레이어 안내다. 네이티브 플레이어의 Bearer도 Nginx가 허용하지만
최초 재생목록뿐 아니라 모든 하위 재생목록·영상 조각·재생 요청에 헤더를 보내야 한다.
쿠키 방식도 영상 HTTP 클라이언트와 쿠키를 공유해야 한다. Nginx는 요청마다 로그인과
카메라 권한을 확인한다. 재생 중 401이면 토큰 갱신 후 플레이어를 다시 연다.
mpegts 녹화는 /api/v1/recordings/{segment_id}/content를 사용하며 video/mp2t다.
모든 녹화를 HLS로 가정하지 않는다. /content는 Range·If-Range, 200·206·416을 지원한다.
Nginx /playback도 Range·If-Range를 전달하지만 실제 형식·탐색 지원은 MediaMTX 응답에 따른다.
미디어 및 프록시 오류는 JSON이 아닐 수 있으므로 HTTP 상태와 Content-Type을 먼저 확인한다.

### 시간·목록·사람 정보
from·to는 시간대가 포함된 RFC 3339이며 함께 지정하면 from < to다.
이벤트는 from 이상·to 미만, 녹화는 요청 구간과 겹치는 항목을 반환한다.
서버는 UTC로 정규화하며 화면에서 현지 시각으로 바꾼다. 목록은 limit(기본 50, 최대 100),
offset을 사용한다. items가 빌 때까지 offset을 증가시키며 총 개수 필드를 가정하지 않는다.
person_id는 (camera_id, tracking_session_id) 안에서만 유효한 추적 번호다.
global_person_id는 여러 카메라의 인물을 연결하는 별도 ID이며 미판정 시 null이다.
confidence는 감지기의 확신 정도(0~1)이며 신원 일치 확률이 아니다.
이벤트 metadata는 확장 가능한 객체다. identity와 analysis 결과는 이벤트 생성 후 갱신되며
기본 블랙박스는 unconfigured다. 알 수 없는 이벤트 유형·metadata 키도 허용한다.
objects는 최신 좌표 조회 API이며 영상 프레임과 정확히 동기화된 추적 스트림이 아니다.
관측 시각·원본 크기로 표시 여부와 좌표를 계산하고 stale=true이면 박스를 지운다.
snapshot_path 등 저장소 상대 경로를 공개 이미지 URL로 조합하지 않는다. 별도 스냅샷 API는 없다.

### FCM 수신 계약
로그인 후 현재 refresh_token과 함께 PUT /api/v1/notifications/devices로 기기를 등록한다.
event_types=null은 모든 이벤트, []는 수신 안 함, 문자열 목록은 지정 유형이다.
refresh 회전은 기존 기기 등록을 유지한다. 등록 요청이 401이면 갱신 후 Body도 최신 refresh로 바꾼다.
FCM 기기 토큰이 변경되면 새 token으로 PUT 요청을 다시 보내 등록을 갱신한다.
기기 ID는 소문자 16진수 32자리다. 새 로그인마다 새 ID를 사용하면 이전 계정의 지연 알림을 구별할 수 있다.
FCM data는 문자열 event_id, user_id, device_id, camera_id, occurred_at을 포함한다.
Android 앱은 알림 권한을 요청하고 cctv_events 채널을 생성한다.
메시지는 notification과 data를 함께 포함한다. 앱 실행 중에는 앱이 알림을 표시하고,
백그라운드에서는 OS가 표시한 알림을 다시 생성하지 않아 중복을 막는다.
알림 탭은 백그라운드 복귀와 앱 종료 후 시작 모두 처리하고, 로그인 초기화 후 상세를 연다.
알림에는 일반 안내만 있으며 영상·JWT·비밀번호는 없다.
중복·지연 수신에 대비해 event_id로 중복 표시를 줄이고 현재 사용자·기기 ID를 확인한 뒤
GET /api/v1/events/{event_id}로 내용을 다시 조회한다. 권한 회수·로그아웃 뒤 도착한 알림을 그대로 신뢰하지 않는다.
발송은 이벤트 생성 시 등록된 수신자만 대상으로 하며 기존 이벤트를 새 기기에 소급 발송하지 않는다.
최대 8회·생성 후 1시간 범위에서 재시도한다. FCM 접수와 단말 도착은 서로 다르다.
Firebase 서버·Android 설정과 실기기 확인이 필요하다.
GET /api/v1/notifications/status는 서버의 푸시 사용 설정만 나타낸다.

### 오류와 명세 한계
일반 오류는 {detail: 문자열}, 422는 {detail: [{type, loc, msg}]}이다.
제어 오류와 일부 충돌은 {error: {code, message, details}}다. 400·422는 입력 수정,
403·404는 권한·자원 확인, 409는 상태 충돌 확인, 429는 Retry-After 이후 재시도한다.
502·503·504에는 제한된 지수 지연을 적용하되 변경 요청의 적용 여부를 먼저 조회한다.
주요 코드: EDGE_OFFLINE, CAPABILITY_UNKNOWN, UNSUPPORTED_VIDEO_PROFILE, CONTROL_TIMEOUT,
MEDIA_CONTROL_UNAVAILABLE, CAMERA_HAS_HISTORY, CAMERA_LIMIT_REACHED. 알 수 없는 코드도 처리한다.
metadata·시스템 상태의 data 내부 필드는 자유 형식이다. 모든 장애 코드나 MediaMTX 미디어 형식을
열거한 명세는 아니다. 이 파일은 실행 서버의 /api/v1/openapi.json에 사용 설명과 응답 형식을
보완해 생성한 문서다. 생성·일치 검사는 파일 끝의 x-generation 명령을 사용한다.
"""


# 짧은 요약은 API 탐색 목록에, 자세한 동작은 각 요청 화면에 표시된다.
OPERATIONS = {
    "login": (
        "로그인",
        "계정 확인 후 토큰 쌍과 쿠키를 발급한다. 반복 실패 시 429와 Retry-After를 반환한다.",
    ),
    "refresh": (
        "토큰 갱신",
        "Body의 refresh_token이 쿠키보다 우선한다. 둘 중 하나가 필요하다. 성공하면 이전 refresh를 폐기하므로 동시 갱신을 피한다.",
    ),
    "logout": (
        "로그아웃",
        "Body 또는 쿠키의 현재 refresh와 Authorization 또는 쿠키의 access를 함께 보낸다. 유효한 토큰만 철회하며 토큰이 없거나 만료된 경우에도 쿠키 삭제 후 204일 수 있다. refresh 철회는 그 계열의 기기·대기 알림도 해제한다. 이미 FCM에 접수된 알림은 취소되지 않는다.",
    ),
    "list_cameras": (
        "카메라 목록",
        "현재 사용자가 볼 수 있는 카메라를 조회한다. 비밀값과 Edge 내부 주소는 공개 응답에서 제외한다.",
    ),
    "create_camera": (
        "카메라 등록",
        "stream_path는 생략하거나 camera_id와 같아야 한다. Edge 연동 시 edge_device_id, edge_management_url, edge_recovery_url, edge_auth_token을 모두 보낸다. 활성 카메라 최대 4개. publish_credentials 비밀번호는 이 응답에서만 받으므로 일치하는 Edge에 안전하게 전달한다.",
    ),
    "update_camera": (
        "카메라 설정 변경",
        "변경할 필드만 보낸다. enabled=false는 새 송출과 Live를 차단하고 기존 송출 연결을 끊는다. Media 제어 실패 시 차단 상태를 유지하므로 상태를 확인하고 재시도한다.",
    ),
    "delete_camera": (
        "카메라 삭제",
        "이력이 있으면 CAMERA_HAS_HISTORY 409로 거부한다. 이력 보존이 필요하면 enabled=false를 사용한다.",
    ),
    "rotate_camera_publish_credentials": (
        "송출 비밀번호 교체",
        "기존 송출자를 끊고 비밀번호를 교체한다. 새 publish_credentials는 한 번만 반환한다. 중간 실패 시 카메라가 비활성 상태로 남을 수 있으므로 상태를 확인한다.",
    ),
    "get_camera": (
        "카메라 조회",
        "현재 사용자에게 해당 카메라의 열람 권한이 있어야 한다.",
    ),
    "get_camera_live": (
        "실시간 영상 주소",
        "url을 재생한다. hls_url은 같은 주소의 호환 필드다. 비활성 카메라는 409. 실제 HLS는 Nginx 경로이며 재생목록과 모든 조각에 인증이 필요하다.",
    ),
    "get_camera_video_profile": (
        "영상 설정 조회",
        "현재값·요청값·지원 profile과 Edge 연결 여부를 반환한다.",
    ),
    "update_camera_video_profile": (
        "영상 설정 변경",
        "hd=720p/30fps/2Mbps, fhd=1080p/30fps/4Mbps다. Edge 적용 후 현재값이 바뀐다. 잠금·적용·복구에 시간이 걸리므로 약 90초의 클라이언트 대기 시간을 고려하고 오류 후 조회로 결과를 확인한다.",
    ),
    "get_camera_status": (
        "카메라 장치 상태",
        "Edge HTTP 연결, 카메라 입력, 중앙 전송 상태는 별개다. 센서가 없는 수치는 null일 수 있다.",
    ),
    "get_live_objects": (
        "최신 객체 좌표",
        "원본 픽셀 기준 bbox=[x1,y1,x2,y2]다. 화면 비율·여백을 반영해 그린다. 최근 3초 이내 관측만 반환하고 없거나 오래됐거나 미래 시각이면 stale=true와 빈 objects를 반환한다. stale일 때 세션·크기·관측 시각 필드는 없다. HLS 프레임과 정확히 동기화되지 않는다.",
    ),
    "list_recordings": (
        "녹화 구간 검색",
        "camera_id·from·to가 필요하다. 요청 시간 구간과 겹치는 녹화를 반환한다. 권한이 있는 카메라만 조회한다.",
    ),
    "get_recording": (
        "녹화 구간 조회",
        "녹화 정보와 그 카메라의 열람 권한을 확인한다.",
    ),
    "get_recording_playback": (
        "녹화 재생 주소",
        "format=mpegts면 공개 /content 주소, 그 외에는 MediaMTX fmp4 Playback 주소를 반환한다. 주소를 직접 조합하거나 모두 HLS라고 가정하지 않는다.",
    ),
    "get_recording_content": (
        "녹화 영상 바이트",
        "Range가 없으면 전체 200, 유효한 단일 Byte Range는 206, 범위를 만족할 수 없으면 416이다. If-Range의 ETag·시각이 현재 파일과 다르면 전체 200을 반환한다.",
    ),
    "list_recovery_jobs": (
        "영상 복구 작업 목록",
        "Edge 장애 구간의 복구 진행 상태와 오류를 조회한다.",
    ),
    "list_events": (
        "이벤트 검색",
        "viewer는 camera_id를 반드시 지정해야 한다. admin은 생략하여 전체를 검색할 수 있다. event_type·from·to는 선택이며 시각 범위는 from 이상·to 미만이다. 인물 연결·분석 결과는 나중에 갱신될 수 있다.",
    ),
    "get_event": (
        "이벤트 상세",
        "푸시의 event_id로도 조회한다. snapshot_path는 공개 URL이 아니다. 연결 녹화는 recording_segment_id 또는 recording_segment_ids로 조회한다.",
    ),
    "list_users": (
        "사용자 목록",
        "사용자 계정과 역할을 조회한다. 비밀번호 해시는 반환하지 않는다.",
    ),
    "create_user": (
        "사용자 생성",
        "admin 또는 viewer 역할을 지정한다. 새 비밀번호는 12자 이상이어야 한다.",
    ),
    "update_user": (
        "사용자 변경",
        "비밀번호·역할·활성 상태 중 변경할 필드만 보낸다. 역할 변경 후 기존 access는 사용할 수 없으므로 토큰 갱신 또는 재로그인이 필요하다.",
    ),
    "get_user_permissions": (
        "사용자 카메라 권한 조회",
        "사용자에게 배정된 카메라 목록을 반환한다.",
    ),
    "set_user_permissions": (
        "사용자 카메라 권한 교체",
        "camera_ids 전체 목록으로 현재 배정을 교체한다. 빈 목록은 모든 배정을 해제한다.",
    ),
    "system_status": (
        "시스템 상태 조회",
        "External과 Data 상태를 반환한다. 여섯 컨테이너 전체·영상·FCM 수신의 성공 판정은 아니다. /system/status와 /admin/system/status는 같은 기능이다.",
    ),
    "push_status": (
        "푸시 설정 여부",
        "enabled는 서버의 PUSH_ENABLED 설정이다. Firebase 연결 성공이나 단말 도착을 확인하지 않는다.",
    ),
    "register_device": (
        "알림 기기 등록·수신 설정",
        "현재 사용자 소유의 유효한 refresh_token이 필요하다. user_id·역할은 서버가 정한다. event_types=null은 전체, []는 없음이다. 토큰 문자열에는 공백이 없어야 한다.",
    ),
    "unregister_device": (
        "알림 기기 등록 해제",
        "현재 사용자 소유 기기만 해제한다. 없는 등록도 204다. 로그아웃은 현재 refresh를 함께 보내 세션까지 철회한다.",
    ),
}


class PublicLiveObject(LiveObject):
    global_person_id: str | None


class FreshLiveObjects(LiveObjects):
    camera_id: str
    stale: Literal[False]
    objects: list[PublicLiveObject] = Field(max_length=100)


class StaleLiveObjects(BaseModel):
    camera_id: str
    stale: Literal[True]
    objects: list[Any] = Field(max_length=0)


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


# Pydantic 모델의 중첩 정의를 공통 components로 옮겨 문서 안의 참조가 해결되게 한다.
def _add_model(schemas: dict[str, Any], model: type[BaseModel]) -> None:
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    schemas.update(schema.pop("$defs", {}))
    schemas[model.__name__] = schema


# 직접 또는 중첩 의존성으로 연결된 관리자 검사를 찾아 문서에 관리자 전용임을 표시한다.
def _admin_dependency(dependant: Any) -> bool:
    return dependant.call is require_admin or any(
        _admin_dependency(child) for child in dependant.dependencies
    )


# 실제 반환 코드에 따라 최신·만료 객체와 알림 응답의 자동 명세 공백을 보완한다.
def _supplement_responses(document: dict[str, Any]) -> None:
    # 자동 명세에서 비어 있는 응답만 실제 Data 반환 형태로 채운다.
    # 기존 서버 모델 자체는 변경하지 않는다.
    schemas = document["components"]["schemas"]
    for model in (FreshLiveObjects, StaleLiveObjects):
        _add_model(schemas, model)
    schemas["PublicLiveObjects"] = {
        "oneOf": [_ref("FreshLiveObjects"), _ref("StaleLiveObjects")],
        "description": "Data get_live_objects의 최신/만료 응답. stale에 따라 필드가 다르다.",
    }
    schemas["PushStatus"] = {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            "provider": {"const": "fcm", "type": "string"},
        },
        "required": ["enabled", "provider"],
    }
    registration = schemas["DeviceRegistration"]
    device_fields = ("device_id", "enabled", "event_types", "platform")
    schemas["DeviceRegistrationResponse"] = {
        "type": "object",
        "properties": {
            name: deepcopy(registration["properties"][name]) for name in device_fields
        },
        "required": list(device_fields),
    }
    for path, method, name in (
        ("/api/v1/cameras/{camera_id}/objects", "get", "PublicLiveObjects"),
        ("/api/v1/notifications/status", "get", "PushStatus"),
        ("/api/v1/notifications/devices", "put", "DeviceRegistrationResponse"),
    ):
        operation = document["paths"][path][method]
        operation["responses"]["200"]["content"]["application/json"]["schema"] = _ref(
            name
        )
        operation["x-schema-source"] = (
            "실제 반환 코드를 기반으로 문서 생성기가 보완한 응답"
        )
    registration["properties"]["event_types"]["description"] = (
        "null=모든 이벤트, []=수신 없음. 각 항목은 ^[a-z][a-z0-9_]{0,127}$이며 중복 제거·정렬 후 저장한다."
    )
    registration["properties"]["refresh_token"]["description"] = (
        "현재 로그인한 사용자 소유의 아직 회전되지 않은 refresh 토큰"
    )


# 일반·제어·검증 오류의 공통 참조를 추가하고 프록시의 비JSON 응답도 문서에 표현한다.
def _supplement_errors(document: dict[str, Any]) -> None:
    schemas = document["components"]["schemas"]
    schemas["DetailError"] = {
        "type": "object",
        "properties": {"detail": {"type": "string"}},
        "required": ["detail"],
    }
    schemas["ControlError"] = {
        "type": "object",
        "properties": {
            "error": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "details": {"type": "object", "additionalProperties": True},
                },
                "required": ["code", "message", "details"],
            }
        },
        "required": ["error"],
    }
    document["components"]["responses"] = {
        "ApiError": {
            "description": "요청·권한·상태·상위 서비스 오류. 일반 detail 또는 제어 error 구조이며 422는 detail 배열이다. 프록시나 처리하지 못한 서버 오류는 JSON이 아닐 수 있다.",
            "content": {
                "application/json": {
                    "schema": {
                        "anyOf": [
                            _ref("DetailError"),
                            _ref("ControlError"),
                            _ref("HTTPValidationError"),
                        ]
                    }
                },
                "text/plain": {"schema": {"type": "string"}},
                "text/html": {"schema": {"type": "string"}},
            },
        },
        "Unauthorized": {
            "description": "인증 정보가 없거나 만료·철회·변경되었다. 현재 refresh로 한 번만 갱신하고 실패하면 다시 로그인한다.",
            "content": {"application/json": {"schema": _ref("DetailError")}},
        },
    }


# 실행 라우트의 복사본에 공개 계약 설명·인증 방식·미디어 범위 헤더를 결합한다.
def build_document() -> dict[str, Any]:
    # create_app/openapi는 라우트만 등록한다. lifespan에 진입하지 않으므로
    # 작업자·DB 클라이언트가 실행되지 않고 .env나 인증 파일도 읽지 않는다.
    application = create_app()
    document = deepcopy(application.openapi())
    document["info"]["title"] = "AI CCTV 공개 API — 모바일 교체 계약"
    document["info"]["description"] = DESCRIPTION.strip()
    document["servers"] = [
        {
            "url": "/",
            "description": "배포된 중앙 HTTPS origin. /api/v1은 각 path에 포함된다.",
        }
    ]
    document["paths"] = {
        path: item
        for path, item in document["paths"].items()
        if path.startswith("/api/v1/")
    }
    document["x-generation"] = {
        "command": "python server/services/external/tools/export_openapi.py",
        "check": "python server/services/external/tools/export_openapi.py --check",
        "source": "server/services/external/app/main.py:create_app().openapi()",
    }
    document["components"]["securitySchemes"] = {
        "HTTPBearer": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "access_token. refresh_token을 Bearer로 사용하지 않는다.",
        },
        "AccessCookie": {
            "type": "apiKey",
            "in": "cookie",
            "name": "ai_cctv_access",
            "description": "기본 access 쿠키명. 배포의 ACCESS_COOKIE_NAME으로 변경할 수 있다.",
        },
    }
    _supplement_responses(document)
    _supplement_errors(document)
    for route in application.routes:
        if not isinstance(route, APIRoute) or route.path not in document["paths"]:
            continue
        summary, description = OPERATIONS[route.endpoint.__name__]
        for method in route.methods:
            operation = document["paths"][route.path][method.lower()]
            operation["summary"] = summary
            admin = _admin_dependency(route.dependant)
            operation["description"] = ("관리자 전용. " if admin else "") + description
            operation["tags"] = [route.path.removeprefix("/api/v1/").split("/")[0]]
            if operation.get("security"):
                operation["security"] = [{"HTTPBearer": []}, {"AccessCookie": []}]
                operation["responses"]["401"] = {
                    "$ref": "#/components/responses/Unauthorized"
                }
            elif route.endpoint.__name__ == "logout":
                # access가 있으면 철회에 사용하지만 없어도 쿠키 삭제 등은 처리한다.
                operation["security"] = [
                    {"HTTPBearer": []},
                    {"AccessCookie": []},
                    {},
                ]
            else:
                # login/refresh는 access 인증을 요구하지 않는다.
                # refresh의 Body-or-Cookie 조건은 security 필수값으로 표현하지 않는다.
                operation["security"] = []
            operation["responses"]["default"] = {
                "$ref": "#/components/responses/ApiError"
            }
            for parameter in operation.get("parameters", []):
                name = parameter["name"]
                if name in {"from", "to"}:
                    parameter["description"] = (
                        "시간대를 포함한 RFC 3339. 예: 2026-09-07T00:00:00Z. 함께 지정하면 from < to."
                    )
                elif name == "offset":
                    parameter["description"] = (
                        "건너뛸 항목 수. items가 빈 목록이면 조회를 마친다."
                    )

    for name in ("login", "refresh"):
        operation = document["paths"][f"/api/v1/auth/{name}"]["post"]
        operation["responses"]["401"] = {"$ref": "#/components/responses/Unauthorized"}
        operation["responses"]["200"]["headers"] = {
            "Set-Cookie": {
                "description": "access·refresh 쿠키를 각각 설정한다. 수명·경로·기본 이름은 위 인증 설명 참조.",
                "schema": {"type": "string"},
            }
        }
    for name in ("refresh", "logout"):
        document["paths"][f"/api/v1/auth/{name}"]["post"]["parameters"] = [
            {
                "in": "cookie",
                "name": "ai_cctv_refresh",
                "required": False,
                "schema": {"type": "string"},
                "description": "Body의 refresh_token이 없을 때 사용한다. REFRESH_COOKIE_NAME 설정으로 이름이 바뀔 수 있다.",
            }
        ]
    document["paths"]["/api/v1/auth/login"]["post"]["responses"]["429"] = {
        "description": "반복 로그인 실패로 일시 지연됨",
        "headers": {
            "Retry-After": {
                "description": "다시 시도하기 전 기다릴 초",
                "schema": {"type": "integer", "minimum": 1},
            }
        },
        "content": {"application/json": {"schema": _ref("DetailError")}},
    }
    content = document["paths"]["/api/v1/recordings/{segment_id}/content"]["get"]
    content["parameters"].extend(
        [
            {
                "name": "Range",
                "in": "header",
                "required": False,
                "description": "단일 Byte Range. 예: bytes=0-1023, bytes=1024-, bytes=-1024",
                "schema": {"type": "string"},
            },
            {
                "name": "If-Range",
                "in": "header",
                "required": False,
                "description": "이전 응답의 ETag 또는 HTTP 날짜. 일치하지 않으면 전체 200.",
                "schema": {"type": "string"},
            },
        ]
    )
    for status in ("200", "206", "416"):
        content["responses"][status]["headers"] = {
            name: {
                "schema": {"type": "string"},
                "description": "상태와 요청에 따라 제공되는 원본 영상 응답 헤더",
            }
            for name in (
                "Accept-Ranges",
                "Content-Range",
                "ETag",
                "Last-Modified",
                "Content-Length",
            )
        }
    return document


class _ReadableDumper(yaml.SafeDumper):
    # 긴 설명만 여러 줄로 표시한다. YAML 별칭을 없애 문서가 독립적으로 읽히게 한다.
    def ignore_aliases(self, data: Any) -> bool:
        return True


# 여러 줄 설명은 YAML 블록 문자열로 출력하여 줄바꿈을 읽기 쉽게 유지한다.
def _represent_string(dumper: yaml.SafeDumper, value: str) -> Any:
    return dumper.represent_scalar(
        "tag:yaml.org,2002:str", value, style="|" if "\n" in value else None
    )


_ReadableDumper.add_representer(str, _represent_string)


# 순서를 유지한 UTF-8 YAML과 재생성 안내를 만들어 같은 코드에서 같은 문서를 출력한다.
def render_document() -> str:
    return (
        "# 자동 생성: python server/services/external/tools/export_openapi.py (직접 수정하지 않음)\n"
        + yaml.dump(
            build_document(),
            Dumper=_ReadableDumper,
            allow_unicode=True,
            sort_keys=False,
            width=100,
        )
    )


# check 모드는 파일을 쓰지 않고 생성 결과와 비교하며 기본 실행은 공개 명세를 갱신한다.
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="파일을 쓰지 않고 현재 코드와의 일치만 확인",
    )
    args = parser.parse_args()
    target = ROOT / "docs" / "openapi.yaml"
    expected = render_document()
    if args.check:
        if not target.exists() or target.read_text(encoding="utf-8") != expected:
            print("OpenAPI differs; run python server/services/external/tools/export_openapi.py")
            return 1
        print("OpenAPI is current")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(expected, encoding="utf-8", newline="\n")
    print("Updated docs/openapi.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
