# Data 서비스

사용자·카메라·이벤트·녹화 정보와 인물 연결·분석·푸시 대기 작업을 SQLite에 저장한다. 중앙 서버 DB를 직접 여는 유일한 서비스다. 다른 컨테이너는 인증된 내부 API로 데이터를 요청한다.

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
| `tools/backup_database.py` | 호스트에서 Data 백업 API를 호출하는 온라인 DB 백업 도구 |
| `tests/` | Data API와 저장 동작 테스트 |

이벤트와 관련 작업은 하나의 트랜잭션으로 저장한다. 중간에 실패하면 함께 취소하여 이벤트만 저장되거나 알림 작업만 남는 일을 막는다. 객체 작업의 공통 요청·응답 계약은 [`lib/ai_cctv_core/contracts/objects.py`](../../../lib/ai_cctv_core/contracts/objects.py)에 있다. DB 구조 변경은 `app/database/migrations/`에 새 버전을 추가한다. 시작할 때 미적용 SQL 파일만 실행하므로 이미 적용한 마이그레이션 파일을 수정해도 기존 DB에는 반영되지 않는다.

## 실행과 인증

중앙 Compose의 `data` 서비스로 실행한다. `/health/live`는 프로세스 상태, `/health/ready`는 DB·저장소 사용 가능 여부다. 이 주소와 내부 API는 외부 사용자에게 공개하지 않는다.

내부 API `/internal/v1`은 `X-Internal-Token`으로 호출자를 구분한다. 기본 비밀 설정 파일은 `server/secrets/data.env`다. `external`, `inference`, `identity`, `analysis`, `media`, `recovery`는 **인증 권한 이름**이며 컨테이너 수를 뜻하지 않는다. Preprocessing은 감지에 `inference`, 인물 연결에 `identity` 권한을 사용한다.

생성된 서비스별 `DATA_*_TOKEN`은 서로 다른 32자 이상의 값이다. 호출 서비스의 토큰은 Data의 해당 권한 토큰과 일치해야 한다. 알 수 없는 토큰은 HTTP 401, 유효하지만 해당 API 권한이 없는 토큰은 HTTP 403을 반환한다.

초기 관리자 계정은 사용자 테이블이 비어 있을 때만, `config.yaml`의 카메라 목록은 카메라 테이블이 비어 있을 때만 생성한다. 이후 계정·카메라 변경은 관리자 API를 사용한다. 다만 설정에 장치 ID·관리 주소·복구 주소와 대응 토큰이 모두 있으면 해당 Edge 연결 정보와 카메라의 장치 연결은 재시작 때 다시 적용된다. 운영 중 연결 정보를 바꿨다면 부트스트랩 설정도 함께 맞춘다.

Edge 녹화 복구와 전체 백업 절차는 [운영과 백업](../../../docs/guide.md#운영과-백업)을 따른다. 온라인 DB 백업 도구는 Data 서비스가 소유하며 저장소 루트에서 `python server/services/data/tools/backup_database.py`로 실행한다. 기본 `server/.env` 또는 `--server-dir` 아래 `.env`를 사용하고 결과는 `DATABASE_DIR/backups`에 저장한다. 설치 도우미의 `config/compose.env`를 자동으로 선택하지 않으며 영상·설정·비밀 파일은 이 백업에 포함하지 않는다.

## 검증

[개발 환경](../../../docs/guide.md#서버-코드-개발)을 준비하고 개발 Compose를 기동한 뒤, 저장소 루트에서 실행한다. 아래 `server/.env`는 개발 전용 설정이다.

```powershell
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml exec data python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/data/tests -q
```

서비스 간 연동·복구·푸시·객체 처리 검증은 [서버 자동 테스트](../../../docs/guide.md#서버-자동-테스트)를 따른다.
