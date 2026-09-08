# Data 서비스

SQLite·이벤트·작업·녹화 정보를 관리합니다. DB를 직접 여는 유일한 서비스이며, `app/database/migrations/`의 버전 순서로 DB를 업그레이드합니다.

## 코드 위치

| 경로 | 책임 |
|---|---|
| `app/main.py` | FastAPI 생성, 시작·종료와 라우터 연결 |
| `app/bootstrap.py` | 저장소 초기화, 초기 관리자·카메라 설정 적용 |
| `app/dependencies.py`, `app/security.py` | 요청 의존성, 내부 서비스 인증·권한 |
| `app/api/` | 사용자·세션·카메라·이벤트·녹화·객체·알림·운영 API |
| `app/schemas.py` | Data 내부 API의 요청 검증 |
| `app/database/connection.py` | SQLite 연결·트랜잭션·마이그레이션·백업 |
| `app/database/repositories/` | 도메인별 SQL과 `DataRepository` 조합 |
| `app/storage/` | 저장 경로 검증, 녹화 인덱싱·정합성 점검·보관 기간 정리 |
| `app/workers/` | 주기적 저장소 관리와 Edge 녹화 복구 |
| `tests/` | Data API와 저장 동작 테스트 |

`DataRepository`는 도메인별 mixin을 명시적으로 조합합니다. 이벤트 저장, 녹화 연결, 푸시 대기열 생성, 객체 분석 작업 생성은 하나의 연결과 트랜잭션에서 실행하며 어느 단계든 실패하면 함께 롤백합니다. 파일 작업·외부 HTTP 요청·Firebase 발송은 해당 트랜잭션 안에서 실행하지 않습니다.

객체 관측과 작업 결과의 공통 계약은 `lib/ai_cctv_core/contracts/objects.py`에 있습니다. DB를 다른 서비스에서 직접 읽거나 쓰지 않고 `/internal/v1` API를 사용합니다.

## 실행과 인증

저장소 루트의 Compose 구성에서 `data`를 실행합니다. 컨테이너 내부 시작 명령은 `uvicorn app.main:app --host 0.0.0.0 --port 8000`입니다. `/health/live`는 프로세스 상태, `/health/ready`는 DB·저장소 사용 가능 여부를 확인합니다.

내부 API에는 `X-Internal-Token`이 필요합니다. `external`, `inference`, `identity`, `analysis`, `media`, `recovery` 용도의 인증키를 분리합니다. `preprocessing` 컨테이너에 감지와 인물 연결이 함께 있어도 각 작업의 API 권한은 각각 `inference`, `identity`로 유지합니다. 권한은 `app/security.py`에 정의되어 있습니다.

수동 Edge 복구 명령은 컨테이너에서 `python -m app.workers.recovery --help`로 확인합니다.

## 검증

개발 Compose로 기동한 뒤 저장소 루트에서 다음을 실행합니다.

```text
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml exec data python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/data/tests -q
```

서비스 간 연동·복구·푸시·객체 처리 테스트는 루트 `tests/automated/`에서 함께 실행합니다. 운영 DB와 운영 저장소에 테스트를 실행하지 않습니다.
