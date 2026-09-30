# 운영 안내

명령은 저장소 루트 또는 설치 폴더에서 실행한다. `C:/path/to/compose.env`는 실제 배포 env로 바꾸고, FCM 사용 시 push env·Compose 파일도 포함한다.

## 서버 관리자 화면

Windows 서버 PC의 PyQt 관리자 화면에서 Edge·카메라 등록, 게시 계정 재발급, 화질 변경과 상태 확인을 수행한다. 최초 검색·Pairing과 서버 시작·중지는 설치 도우미를 사용한다. 게시 계정을 재발급하면 내려받은 JSON을 [Edge에 적용](../edge/README.md#수동-연결)한다.

초기 설정에 Edge 주소·토큰을 넣었다면 장치 변경 시 해당 설정도 갱신한다. 재시작 때 적용되는 조건은 [Data README](../server/services/data/README.md)를 따른다.

### PC 실시간 영상 지연

PC 화면은 인증된 HLS를 PyAV로 재생한다. 2초 세그먼트와 FFmpeg 기본 시작 위치
(`live_start_index=-3`)를 함께 쓰면 최신 완료 구간보다 두 조각, 즉 4초 전부터
재생하며 세그먼트 완성 대기까지 포함해 약 6초 이상 늦어질 수 있다.
현재는 1초 세그먼트와 최신 완료 조각(`-1`)을 사용하고, 초기 스트림 분석 시간을
제한한다. 로컬 웹캠도 1초마다 키프레임을 보내며 Edge 기본 x264 설정과 일치한다.
세그먼트 길이는 실제 카메라의 키프레임 간격보다 짧아질 수 없으므로 다른 인코더를
사용하면 재생목록의 `EXTINF` 길이도 확인한다.

PC의 디코딩 대기열은 최대 30장으로 제한하고 가득 차면 오래된 프레임을 즉시
버린다. UI 정체·재접속 뒤에는 오래된 프레임과 재생 시간 기준을 정리한다.
`video diagnostics`의 `queue`·`dropped` 및 `video render diagnostics`의
`average_ms`로 화면 처리 병목을 확인할 수 있다. 이 값은 촬영부터 표시까지의
지연 측정값은 아니다.

업데이트 시 관리자 앱을 다시 실행하고, 변경한 MediaMTX 설정을 서버에 반영한다.
설치 EXE를 사용한다면 변경된 코드로 빌드한 설치본이 필요하다. 이미 송출 중인
로컬 웹캠도 송출을 다시 시작해야 새 키프레임 간격이 적용된다.
실측은 카메라로 초시계를 촬영해 원본과 PC 화면의 시간을 비교하며, 시작 직후와
몇 분 후 및 연결 복구 후를 각각 확인한다. 완성된 조각을 받는 HLS 방식이므로
네트워크·인코더 상태에 따라 지연이 남으며 1초 미만을 보장하지 않는다.

회귀 테스트는 데스크톱 의존성이 설치된 Python 3.11에서
`python -m unittest discover -s server/setup/install_helper/tests -v`로 실행한다.
FFmpeg가 PATH에 있으면 가상 HLS를 만들어 실제 PyAV·MediaBridge의 재생 시작
위치도 검증한다.

## 상태 확인

```powershell
docker compose --env-file C:/path/to/compose.env -f server/compose.yml ps
docker compose --env-file C:/path/to/compose.env -f server/compose.yml logs --tail 100
```

PyQt 관리자 화면 또는 `GET /api/v1/system/status`에서 상태를 확인한다. Analysis 직접 검사는 포함하지 않으므로 해당 컨테이너의 상태·로그를 확인한다.

| 내부 검사 | 의미 |
|---|---|
| Nginx `/healthz`, Python `/health/live` | 응답·프로세스 생존 |
| Data `/health/ready` | DB·저장소·작업자 상태; 작업자 장애는 503 |
| External `/health/ready` | 내부 Nginx를 통한 Data 연결 |
| Preprocessing `/health/ready` | Data 연결 실패는 503; 모델·영상·이벤트 오류는 200 `degraded`일 수 있음 |
| Analysis `/health/ready` | 작업기 미준비·정지는 503; 최근 처리 오류는 200 `degraded`일 수 있음 |

`backend`, `model_ready`, `last_error`, 카메라의 `frame_stale`과 큐 상태를 확인한다. `unconfigured`는 선택한 플러그인의 미설정 상태다. 정상 판정에는 로그인 → 실시간 영상 → 새 이벤트 → 녹화 재생과, 설정한 경우 실제 푸시 수신까지 확인한다.

## 운영과 백업

### 전체 백업

1. Edge 로컬 녹화 상태를 확인하고 중앙 서비스를 정상 중지한다.
2. 아래 자료를 같은 시점의 세트로 복사한다.
3. 서비스를 다시 시작하고 실제 기능을 확인한다.

```powershell
docker compose --env-file C:/path/to/compose.env -f server/compose.yml down
# 이 시점에 아래 경로를 백업한다.
docker compose --env-file C:/path/to/compose.env -f server/compose.yml up -d --wait
```

| 백업 대상 | 포함 항목 |
|---|---|
| 데이터 | `DATABASE_DIR`, `RECORDINGS_DIR`, `RECOVERED_DIR`, `SNAPSHOTS_DIR`의 이미지·outbox |
| 설정·인증 | 배포 env, `CONFIG_FILE`, 서비스별 비밀 파일, push.env·Firebase 계정 |
| 모델·TLS | `MODELS_DIR`, `CERTS_DIR` |
| 복원 기준 | 코드·이미지 버전, release manifest, 모델 해시, 경로·권한 |

실행 중인 SQLite 파일을 개별 복사하지 않는다. 소스 배포의 온라인 DB 전용 백업은 [Data 백업 도구](../server/services/data/README.md#실행과-인증)를 따른다.

### 백업 복원

서비스 중지 → 현재 자료 별도 보존 → 같은 백업 세트의 코드·이미지·DB·파일·설정 복원 → 권한·기동·과거 녹화 재생 확인 순서로 진행한다. DB 자동 downgrade는 지원하지 않는다.

## 업데이트

1. 전체 백업 후 기존 프로젝트 이름·데이터 경로를 기록한다.
2. 새 설치 파일 또는 소스를 배치한다.
3. 도우미의 **서버 시작 / 업데이트 적용** 또는 아래 명령으로 컨테이너를 재생성한다.

```powershell
docker compose --env-file C:/path/to/compose.env -f server/compose.yml up -d --build --wait --remove-orphans
```

단순 재시작은 새 이미지·환경을 반영하지 않는다. Data 기동 시 DB 마이그레이션이 적용되므로 실패 시 이전 이미지와 같은 시점의 백업을 함께 복원한다.

구형 inference·identity 설정을 이관할 때만 Python 3.11로 `python server/setup/tools/enable_object_processing.py --server-dir server --env-file C:/path/to/compose.env`를 실행한다. 기존 토큰은 보존되며 구형 단일 secrets.env는 수동 이전이 필요하다.

TLS 갱신은 `CERTS_DIR`의 인증서·키 교체 후 같은 Compose 명령에 `exec -T nginx nginx -t`, `exec -T nginx nginx -s reload`를 차례로 실행한다.

## 녹화 복구

자동 작업은 관리자 API `GET /api/v1/recovery-jobs`에서 확인한다. 카메라를 다른 Edge로 옮겨도 기존 복구 작업에는 원래 장치가 필요하다.

수동 복구는 Edge에 원본이 남아 있을 때 사용한다. [내보낸 Edge 토큰](../edge/README.md#수동-연결)을 환경변수로 전달하고 UTC 기간을 최대 24시간으로 지정한다.

```powershell
$env:EDGE_RECOVERY_TOKEN = (Get-Content -Raw -LiteralPath 'C:/secure/edge-001-control.token').Trim()
try { docker compose --env-file C:/path/to/compose.env -f server/compose.yml exec -T -e EDGE_RECOVERY_TOKEN data python -m app.workers.recovery --edge-url http://192.0.2.41:8002 --camera-id cam-001 --start 2026-09-07T00:00:00Z --end 2026-09-07T01:00:00Z } finally { Remove-Item Env:EDGE_RECOVERY_TOKEN }
```

## 보관 정책

기본 보관 기간은 `config.yaml`의 `recording.retention_days=7`이다. Data에 직접 전달한 `DATA_RETENTION_DAYS`가 우선한다.

| 자료 | 정리 기준 |
|---|---|
| 중앙·복구 녹화 | 기한 경과 파일; 쓰기 중 파일 제외 |
| 이벤트·이미지 | 기한이 지나고 작업·푸시·outbox 등의 보호 참조가 없을 때 삭제; 새 이미지는 최소 24시간 보호 |
| 미처리 객체 작업 | 기본 30일(`DATA_JOB_MAX_AGE_DAYS`) 후 `failed/OBJECT_RETENTION_EXPIRED`; 유효 임대는 만료까지 보호 |
| 전송 대기·격리 이벤트 | 자동 폐기하지 않음 |
| 인물 연결·gallery | 미사용 연결은 보관 기한 후 정리; gallery는 최근 1,800초·최대 5,000개 |
| 세션·푸시·복구 이력 | 만료·완료 및 보관 기한에 따라 정리 |
| 백업·모델·설정·비밀 파일 | 자동 삭제하지 않음 |

`/snapshots/.pending-observations.json`은 미전송 이미지를 보호한다. 누락·손상·120초 경과·`complete:false`이면 이벤트·이미지 정리를 보류한다. 목록을 수동으로 비우지 않는다. DB 행 삭제는 DB 파일을 즉시 축소하지 않는다.

## 장애 대응

| 증상 | 확인·조치 |
|---|---|
| 게시·영상 인증 실패 | 카메라 게시 계정, 사용자 권한, 갱신 토큰 확인 |
| 모델 오류·`frame_stale` | 모델 경로·호환성, RTSP 연결, 메모리·추론 시간 확인 |
| DB busy·복구 작업자 오류 | Data 작업자 오류, 디스크, 장시간 DB 쓰기 확인 |
| `RECOVERY_JOB_TIMEOUT` | 원래 Edge, 전송 속도·구간, 전체 제한(기본 1,800초) 확인 |
| `RECOVERY_PROCESS_DID_NOT_STOP` | 이전 자식 종료 확인 후 서비스 재시작 |
| `EVENT_STORAGE`·`EVENT_OUTBOX_FULL` | 이미지 저장소 권한·용량, Data 연결, pending/rejected/waiting 확인 |
| `EVENT_REJECTED` | 계약 오류 해결 후 아래 CLI로 격리 이벤트 재시도 |
| `OUTBOX_MANIFEST_*` | Preprocessing·공유 경로·권한과 보호 목록 갱신 확인 |
| 객체 처리 실패 | 이벤트 `metadata.<stage>.result.error_code`, 모델·임대 상태 확인 |
| 푸시 미수신 | 기기 등록·세션·FCM 설정과 External 작업자 확인 |

```powershell
docker compose --env-file C:/path/to/compose.env -f server/compose.yml exec preprocessing python -m server.services.preprocessing.app.outbox_admin list
docker compose --env-file C:/path/to/compose.env -f server/compose.yml exec preprocessing python -m server.services.preprocessing.app.outbox_admin retry --sequence 123
```

## 버전별 인수·복원 기록

[릴리스 기록](../server/tools/README.md#배포-릴리스-기록)에 이미지 ID·모델 해시·장비·날짜와 다음 결과를 남긴다.

- 설치·업데이트 후 계정·키·DB 유지, 로그인·영상·이벤트·푸시 동작
- 동시 처리의 지연·RAM·큐 증가, 연결 단절·모델 오류 후 복구
- 보관 정리와 대기 이미지 보호, 별도 저장소에 백업 복원 후 과거 영상 재생
