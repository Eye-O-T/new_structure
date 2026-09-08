# Data 서비스

사용자·카메라·이벤트·녹화 정보와 분석·푸시 대기 작업을 SQLite에 저장한다. DB를 직접 여는 유일한 서비스다. 다른 컨테이너는 인증된 내부 API로 데이터를 요청한다.

## 코드 위치

| 경로 | 책임 |
|---|---|
| `app/main.py`, `app/bootstrap.py` | 서버 시작, 저장소·관리자·카메라 초기화 |
| `app/security.py` | 내부 서비스 인증·권한 |
| `app/api/` | 사용자·세션·카메라·이벤트·녹화·객체·알림·운영 API |
| `app/schemas.py` | 내부 API 요청 검증 |
| `app/database/` | DB 연결·SQL·버전별 구조 변경·백업 |
| `app/storage/` | 저장 경로 검증, 녹화 인덱싱·정합성 점검·보관 기간 정리 |
| `app/workers/` | 주기적 저장소 관리와 Edge 녹화 복구 |
| `tests/` | Data API와 저장 동작 테스트 |

이벤트와 관련 작업은 하나의 트랜잭션으로 저장한다. 중간에 실패하면 함께 취소하여 이벤트만 저장되거나 알림 작업만 남는 일을 막는다. DB 구조 변경은 `app/database/migrations/`에 새 버전을 추가한다.

## 실행과 인증

중앙 Compose의 `data` 서비스로 실행한다. `/health/live`는 프로세스 상태, `/health/ready`는 DB·저장소 사용 가능 여부다. 이 주소와 내부 API는 외부 사용자에게 공개하지 않는다.

내부 API `/internal/v1`은 `X-Internal-Token`으로 호출자를 구분한다. 기본 비밀 설정 파일은 `server/secrets/data.env`다. `external`, `inference`, `identity`, `analysis`, `media`, `recovery`는 **인증 권한 이름**이며 컨테이너 수를 뜻하지 않는다. Preprocessing은 감지에 `inference`, 인물 연결에 `identity` 권한을 사용한다.

Edge 녹화 복구와 백업 명령은 [운영과 백업](../../../README.md#운영과-백업)을 따른다.

## 검증

[개발 환경](../../../README.md#서버-코드-개발)을 준비하고 개발 Compose를 기동한 뒤, 저장소 루트에서 실행한다. 아래 `server/.env`는 개발 전용 설정이다.

```text
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml exec data python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/data/tests -q
```

서비스 간 연동·복구·푸시·객체 처리 검증은 [서버 자동 테스트](../../../README.md#서버-자동-테스트)를 따른다.
