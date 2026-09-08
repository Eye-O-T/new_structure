# Preprocessing

MediaMTX 영상에서 사람을 감지·추적한다. 사람 영역 이미지는 공유 저장소에 저장하고 바운딩 박스·카메라 내 `person_id`·이미지 경로를 Data에 전달한다. 카메라 간 `global_person_id`를 연결하는 작업도 같은 컨테이너에서 별도로 실행한다. 인물 연결 실패가 감지를 중단시키지 않는다.

| 위치 | 수정할 내용 |
|---|---|
| `app/` | 영상 수신·카메라별 실행·이벤트와 좌표 전달·상태 |
| `processors/detection/contracts.py` | 감지기 입력·출력 계약 |
| `processors/detection/yolo.py` | YOLO/ByteTrack 기본 감지기 |
| `processors/identity/` | 전역 인물 연결 블랙박스 |

기본 감지기는 YOLO/ByteTrack이며 호환 모델 파일이 필요하다. 인물 연결은 구현 자리만 제공하는 블랙박스로, `unconfigured`를 반환하고 전역 ID를 만들지 않는다. 바운딩 박스는 영상에 직접 합성하지 않고 모바일이 전달받은 좌표로 화면 위에 그린다.

`DETECTION_PLUGIN`과 `IDENTITY_PLUGIN`에 `패키지.모듈:팩토리`를 지정해 구현을 교체한다. **[교체 규약](../../../docs/SRS_interface_preprocessing.md)**에서 입력·출력·재시도·배포 방법을 확인한다. SQLite는 직접 열지 않는다.

## 실행과 검증

중앙 Compose로 실행한다. 감지 설정은 `server/config/config.yaml`의 `inference`, 모델·플러그인 선택은 `server/.env`, 인증키는 `server/secrets/preprocessing.env`가 기본 위치다. 설정 준비는 [소스 배포](../../../README.md#소스-배포)를 따른다.

[개발 환경](../../../README.md#서버-코드-개발)을 준비하고 개발 Compose를 기동한 뒤, 저장소 루트에서 실행한다. 아래 `server/.env`는 개발 전용 설정이다.

```powershell
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml exec preprocessing python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/preprocessing/tests -q
```

모델·영상 오류와 블랙박스 상태는 [상태 확인](../../../docs/architecture.md#상태-확인)을 따른다.
