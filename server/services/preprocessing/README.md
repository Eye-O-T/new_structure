# Preprocessing

감지와 전역 인물 연결을 하나의 컨테이너에서 독립 작업기로 실행한다. 인물 연결 실패가 감지를 중단시키지 않는다.

| 위치 | 수정할 내용 |
|---|---|
| `app/` | 영상 수신·카메라별 실행·이벤트와 좌표 전달·상태 |
| `processors/detection/contracts.py` | 감지기 입력·출력 계약 |
| `processors/detection/yolo.py` | YOLO/ByteTrack 기본 감지기 |
| `processors/identity/` | 전역 인물 연결 블랙박스 |

`DETECTION_PLUGIN`과 `IDENTITY_PLUGIN`에 `패키지.모듈:팩토리`를 지정해 교체한다. 인물 연결 기본값은 `unconfigured`이며 전역 ID를 만들지 않는다. **[객체 처리 계약](../../../docs/SRS_interface_preprocessing.md)**에서 컨테이너의 입출력·재시도·배포 규약을 확인한다.

설정은 `DATA_INFERENCE_TOKEN`, `DATA_IDENTITY_TOKEN`, `DATA_SERVICE_URL`, `SNAPSHOTS_ROOT`, `MEDIA_READ_USERNAME/PASSWORD`를 사용한다. `INFERENCE_*`는 기존 감지 설정 이름이다. SQLite는 직접 열지 않는다.

[개발 환경](../../../README.md#개발과-검증)을 준비한 뒤 저장소 루트에서:

```powershell
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml exec preprocessing python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/preprocessing/tests -q
```

실행은 중앙 Compose를 사용한다. 모델·영상 오류 진단은 [운영 상태](../../../docs/architecture.md#상태-확인)를 따른다.
