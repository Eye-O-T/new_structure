# 중앙 저장소 백업과 복구

## 소유권

- `database/`: Data Service만 read/write
- `recordings/`: MediaMTX read/write, Data Service read/write
- `recovered/`: Host에서는 별도 bind source이며 Data 컨테이너 안에서는
  `/recordings/recovered`로 마운트
- `snapshots/`: Inference read/write, Data Service read/write
- `models/`: Inference read-only

Data Service의 쓰기 권한은 새 미디어를 생성하기 위한 것이 아니다. Data Service가
보존 기간 삭제와 DB-파일 reconciliation을 함께 수행하고, readiness에서 저장소
가용성을 확인하기 위해 필요하다.

Data Service는 시작 시와 주기 점검에서 `deleting` 상태를 다시 처리한다. 파일이
남아 있으면 삭제를 재시도하고 이미 없으면 DB를 `deleted`로 수렴시킨다. 또한
MediaMTX 완료 Hook이 Data/Nginx 장애로 끝내 전달되지 않았더라도 표준
`<camera_id>/YYYY/MM/DD/<UTC>.mp4` 경로이고 settle 기간이 지난 파일은 파일명·mtime을
검증해 `central` Segment로 멱등 인덱싱한다. 형식이 맞지 않는 파일은 임의로
신뢰하지 않고 `orphaned` 진단 목록에 남긴다.

## 안전한 백업

실행 중인 SQLite의 `.db`, `-wal`, `-shm` 파일을 개별 파일 복사로 백업하지
않는다. 실행 중에는 Data Service의 SQLite backup API를 호출하는 다음 명령을
사용한다.

```bash
python server/scripts/backup_database.py
```

백업은 `DATABASE_DIR/backups`에 생성된다. 이 명령은 SQLite만 일관된 snapshot으로
만들며 녹화와 snapshot 파일은 복사하지 않는다. 녹화와 snapshot까지 보존하려면
아래 정상 정지 절차로 하나의 backup set을 만들거나, 별도 backup 도구가 DB 백업
파일과 미디어 경로를 같은 recovery point로 관리하도록 구성한다.

Data Service가 시작되지 않는 장애 상황에서는 다음 보수적 절차를 쓴다.

1. Edge 로컬 백업이 동작하는지 확인한다.
2. `docker compose down`으로 정상 정지한다.
3. `config`, `secrets`, `database`, `recordings`, `snapshots`, `models`를 같은
   backup set으로 복사한다.
4. 복사 완료 후 Compose를 다시 시작한다.
5. DB integrity와 녹화 reconciliation을 실행한다.

## Edge 구간 복구

정상 운영에서는 별도 상시 컨테이너를 추가하지 않고 Data Service 내부 Worker가
복구 작업을 자동 처리한다. Edge Publisher의 `central_connection_lost`를 수집하면
`detected`, 실제 게시 복구를 수집하면 `waiting_for_recovery`가 되고, Worker가
`downloading`, `indexing`, `completed` 순서로 진행한다. 실패는 `failed`와 안전한
오류를 기록하고 설정된 최대 횟수까지 지수 Backoff로 재시도한다. 같은 장애를 여러
수집기가 순서가 바뀌어 보고해도 시작 최솟값과 종료 최댓값을 병합한다. Restore 뒤 기본 15초 settle 기간을 두어 Edge splitmux가 마지막 Segment를 닫은 다음에만 Job을 claim하며, 더 늦은 Restore 경계가 들어오면 종료와 claim 시각을 함께 연장한다. Inference
소비자의 `inference_stream_lost/restored`는 Edge 업로드 단절이 아니므로 복구 작업을
만들지 않는다.

Coordinator는 Edge가 생성한 10초 MPEG-TS manifest를 조회하고, 각 파일을 순차
다운로드하여 파일 크기와 SHA-256을 확인한 뒤
`RECORDINGS_ROOT/recovered/<camera_id>/YYYY/MM/DD/`에 원자적으로 배치하고 Data 내부 HTTP
API에 `source=edge_recovery`로 등록한다. 같은 명령을 다시 실행해도 파일 checksum과
idempotency key로 중복 등록하지 않는다. 복구 성공이 Edge 파일을 삭제하지는 않는다.

다음 one-shot CLI는 자동 Worker를 대신하는 정상 운영 경로가 아니다. 장애 Event가
유실되었거나 운영자가 특정 UTC 구간을 다시 수집해야 할 때만 Data 컨테이너에서
수동 보정 도구로 실행한다.

Recovery token은 명령행 인자로 전달하지 않는다. 다음 예시는 host의 보호된 파일을
명령 프로세스 환경으로만 읽고, `docker compose exec -e NAME`에는 변수 이름만
전달한다. Data 내부 호출에는 `data.env`의 Recovery 전용
`DATA_RECOVERY_TOKEN`만 사용한다. 신규 Compose 배포는 결합 `secrets.env`를 허용하지
않으며, Legacy 결합 Token fallback은 Compose 밖의 직접 개발·테스트에만 남겨 둔다.

```bash
EDGE_RECOVERY_TOKEN="$(< /secure/ai-cctv/cam-001-recovery.token)" \
docker compose --env-file server/.env -f server/compose.yml exec -T \
  -e EDGE_RECOVERY_TOKEN data \
  python -m app.recovery_coordinator \
    --edge-url http://192.0.2.41:8002 \
    --camera-id cam-001 \
    --start 2026-08-22T08:00:00Z \
    --end 2026-08-22T09:00:00Z
```

Edge endpoint는 최대 24시간 범위를 허용한다. 더 긴 장애 구간은 24시간 이하로
나누어 순서대로 실행한다. 운영 전에는 Edge 주소가 신뢰된 관리망에 있고 TCP 8002가
인터넷에 공개되지 않았는지 확인한다. 이 명령은 Bearer manifest/개별 파일 계약을
사용하며, legacy prototype의 인증 없는 `/recover` ZIP API는 사용하지 않는다.

## 복구

1. 현재 runtime을 덮어쓰지 말고 별도 격리 경로로 이동한다.
2. 동일 release image와 config schema를 준비한다.
3. database와 파일 directory를 복원한다.
4. Compose를 시작하고 migration 결과를 확인한다.
5. SQLite integrity, file existence와 recording index reconciliation을 수행한다.
6. 정상 확인 뒤 격리본의 삭제 여부를 별도로 결정한다.

삭제나 reinstall은 runtime data 삭제를 암묵적으로 포함하지 않는다.
