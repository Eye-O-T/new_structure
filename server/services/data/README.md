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

중앙 Compose의 `data` 서비스로 실행한다. `/health/live`는 프로세스 상태, `/health/ready`는 DB·저장소 사용 가능 여부와 복구·보관 작업자 상태를 보고한다. 작업자가 죽거나 최근 주기에서 오류가 나면 503으로 드러내며 DB busy·실패 기록 저장 오류는 재시도한다. 이 주소와 내부 API는 외부 사용자에게 공개하지 않는다.

내부 API `/internal/v1`은 `X-Internal-Token`으로 호출자를 구분한다. 기본 비밀 설정 파일은 `server/secrets/data.env`다. `external`, `inference`, `identity`, `analysis`, `media`, `recovery`는 **인증 권한 이름**이며 컨테이너 수를 뜻하지 않는다. Preprocessing은 감지에 `inference`, 인물 연결에 `identity` 권한을 사용한다.

생성된 서비스별 `DATA_*_TOKEN`은 서로 다른 32자 이상의 값이다. 호출 서비스의 토큰은 Data의 해당 권한 토큰과 일치해야 한다. 알 수 없는 토큰은 HTTP 401, 유효하지만 해당 API 권한이 없는 토큰은 HTTP 403을 반환한다.

로그아웃은 사용자와 로그인 계열(`family_id`, access JWT의 `sid`) 단위로 처리한다. `DELETE /internal/v1/tokens/refresh/{jti}`는 해당 계열에서 이미 회전된 후속 토큰과 단말 등록도 함께 폐기한다. 토큰 행은 만료 후 보관 정리까지 남겨 지연된 로그아웃·갱신이 폐기 사실을 확인하게 한다. External 전용 `GET /internal/v1/tokens/families/{family_id}?user_id=...`는 `{active: bool}`을 반환하며, 같은 주소의 `DELETE`는 계열을 멱등하게 폐기한다. 다른 사용자·로그인의 계열에는 영향을 주지 않는다.

초기 관리자 계정은 사용자 테이블이 비어 있을 때만, `config.yaml`의 카메라 목록은 카메라 테이블이 비어 있을 때만 생성한다. 이후 계정·카메라 변경은 관리자 API를 사용한다. 다만 설정에 장치 ID·관리 주소·복구 주소와 대응 토큰이 모두 있으면 해당 Edge 연결 정보와 카메라의 장치 연결은 재시작 때 다시 적용된다. 운영 중 연결 정보를 바꿨다면 부트스트랩 설정도 함께 맞춘다.

복구 작업은 장애 이벤트를 저장할 때 연결된 `edge_device_id`를 보존한다. 카메라를 새 Edge에 연결해도 이전 작업은 원래 장치의 주소·인증값을 사용하고, 해당 이벤트·복구 이력이 남아 있는 동안 이전 장치 정보도 유지한다. 원래 장치의 주소나 토큰이 바뀌면 그 장치 ID의 내부 등록 정보를 수정한다. 장치 출처를 확인할 수 없는 작업은 새 장치로 임의 전환하지 않고 `RECOVERY_ORIGIN_UNAVAILABLE`로 실패한다. 마이그레이션 `009` 이전 이력은 업그레이드 시점에 남아 있는 연결로만 출처를 채울 수 있으므로, 이미 교체된 장치의 과거 이력은 운영자가 확인해야 한다.

자동 복구의 한 작업은 별도 자식 프로세스에서 실행하며, 전체 기한은 기본 1,800초(`RECOVERY_JOB_TIMEOUT_SECONDS`)다. HTTP 무응답 제한 `RECOVERY_TIMEOUT_SECONDS` 기본 30초와 별개로 느린 응답·다운로드·해시 처리 전체에 적용한다. 기한 초과는 `RECOVERY_JOB_TIMEOUT`으로 재시도 정책에 기록하고, 서버 종료 시에는 `RECOVERY_INTERRUPTED`로 남긴다. 부모는 자식 종료와 해당 작업의 `.part` 정리를 확인한 뒤 다음 작업을 받는다. 검증이 끝난 영상은 남겨 재시도에 재사용한다. 자식 종료 확인에는 최대 약 2.2초, DB 상태 기록에는 SQLite 대기 제한에 따른 추가 시간이 필요하므로 전체 기한은 HTTP 응답의 절대 종료 시각을 보장하지 않는다.

운영체제가 종료·강제 종료 요청 후에도 자식을 살아 있는 상태로 보고하면 복구 worker는 `RECOVERY_PROCESS_DID_NOT_STOP` 오류와 readiness 503을 유지하며 후속 작업 claim을 중단한다. 실행 중 파일과 작업 상태를 보존해 중복 쓰기를 막는다. 운영자는 해당 프로세스와 저장장치 상태를 확인하여 종료한 뒤 Data 서비스를 재시작해야 한다.

Edge 녹화 복구와 전체 백업 절차는 [운영과 백업](../../../docs/guide.md#운영과-백업)을 따른다. 온라인 DB 백업 도구는 Data 서비스가 소유하며 저장소 루트에서 `python server/services/data/tools/backup_database.py`로 실행한다. 기본 `server/.env` 또는 `--server-dir` 아래 `.env`를 사용하고 결과는 `DATABASE_DIR/backups`에 저장한다. 설치 도우미의 `config/compose.env`를 자동으로 선택하지 않으며 영상·설정·비밀 파일은 이 백업에 포함하지 않는다.

## 보관과 조회

보관 정리는 녹화뿐 아니라 이벤트·완료 작업·미사용 인물 연결·토큰·삭제 기록에도 적용된다. 미처리 작업은 기본 30일(`DATA_JOB_MAX_AGE_DAYS`) 후 최종 실패로 전환하며 유효한 임대는 만료까지 보호한다. 최종 실패와 공개 이벤트 metadata를 같은 트랜잭션에서 맞추고, 조회에서도 작업 상태를 반영한다. 스냅샷의 미전송 참조는 Preprocessing이 원자 교체하는 `.pending-observations.json`으로 보호한다. 목록이 없거나 120초를 넘으면 이벤트·이미지 정리를 보류한다. 이미지 순회는 실행당 최대 5,000개 엔트리·2초, 삭제는 최대 1,000개로 제한하며 진행 위치를 DB에 저장한다. outbox DB·WAL·일반 파일은 삭제 대상이 아니다. 자세한 기한·예외·장애 복구는 [운영 기준](../../../docs/operations.md)을 따른다.

이벤트 조회는 기존 offset과 함께 `snapshot_max_id`·`cursor`·`order`를 지원한다. 첫 응답의 경계와 다음 커서를 같은 필터·정렬로 전달하면 조회 중 새 이벤트가 생겨도 페이지가 밀리지 않는다. 현재 결과의 스냅샷 격리를 뜻하지는 않으며 보관 만료로 삭제된 항목은 다음 페이지에서 빠질 수 있다.

완료 녹화 등록과 관련 이벤트 연결은 같은 SQLite 트랜잭션에서 확정한다. 연결 중 실패하면 등록도 취소되며, 완료 통지 재전송은 기존 녹화에 연결을 멱등하게 보강한다. 파일 대조 작업은 구형 버전에서 남은 연결 누락도 실행당 최대 1,000개씩 회복한다.

## 검증

[개발 환경](../../../docs/guide.md#서버-코드-개발)을 준비하고 개발 Compose를 기동한 뒤, 저장소 루트에서 실행한다. 아래 `server/.env`는 개발 전용 설정이다.

```powershell
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml exec data python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/data/tests -q
```

서비스 간 연동·복구·푸시·객체 처리 검증은 [서버 자동 테스트](../../../docs/guide.md#서버-자동-테스트)를 따른다.
