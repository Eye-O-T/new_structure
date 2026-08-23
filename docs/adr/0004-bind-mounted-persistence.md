# ADR-0004: 사용자 선택 경로의 bind mount

- 상태: Accepted
- 날짜: 2026-08-22

## 목차

- [결정](#결정)
- [결과](#결과)

## 결정

SQLite, 녹화 영상, 복구 영상, snapshot, model과 TLS certificate는
Configurator가 생성한 `DATABASE_DIR`, `RECORDINGS_DIR`, `RECOVERED_DIR`,
`SNAPSHOTS_DIR`, `MODELS_DIR`, `CERTS_DIR`을 long-syntax bind mount로 연결한다.
Configurator가 Windows와 Linux의 실제 Host 경로를 생성하고 검증한다.

## 결과

- SQLite directory는 Data Service만 read/write한다.
- MediaMTX는 recordings를 read/write한다. Data Service도 retention 삭제와
  reconciliation을 담당하므로 recordings와 snapshots에 read/write한다.
- Inference는 models를 read-only, snapshots를 read/write한다.
- `docker compose down`은 데이터를 지우지 않는다.
- `down -v`, runtime directory 삭제와 uninstall data reset은 일반 중지 명령에
  포함하지 않는다.
