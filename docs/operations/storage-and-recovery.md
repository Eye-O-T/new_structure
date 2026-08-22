# 중앙 저장소 백업과 복구

## 소유권

- `database/`: Data Service만 read/write
- `recordings/`: MediaMTX read/write, Data Service read/write
- `snapshots/`: Inference read/write, Data Service read/write
- `models/`: Inference read-only

Data Service의 쓰기 권한은 새 미디어를 생성하기 위한 것이 아니다. Data Service가
보존 기간 삭제와 DB-파일 reconciliation을 함께 수행하고, readiness에서 저장소
가용성을 확인하기 위해 필요하다.

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

Recovery Coordinator는 별도 상시 컨테이너가 아니다. 실행 중인 Data 컨테이너에서
필요한 시간 구간에 한해 one-shot CLI로 실행한다. Edge가 생성한 10초 MPEG-TS
manifest를 조회하고, 각 파일을 순차 다운로드하여 SHA-256을 확인한 뒤
`RECORDINGS_ROOT/<camera_id>/YYYY/MM/DD/`에 원자적으로 배치하고 Data 내부 HTTP
API에 `source=edge_recovery`로 등록한다. 같은 명령을 다시 실행해도 파일 checksum과
idempotency key로 중복 등록하지 않는다. 복구 성공이 Edge 파일을 삭제하지는 않는다.

Recovery token은 명령행 인자로 전달하지 않는다. 다음 예시는 host의 보호된 파일을
명령 프로세스 환경으로만 읽고, `docker compose exec -e NAME`에는 변수 이름만
전달한다. Data 내부 token은 컨테이너의 기존 `secrets.env`에서 읽는다.

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
