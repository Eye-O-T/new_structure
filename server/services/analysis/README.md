# Analysis

객체 크롭을 받아 metadata를 추가하는 독립 컨테이너다.

| 위치 | 수정할 내용 |
|---|---|
| `app/` | 작업기 시작·종료, Data 연결, 상태 확인 |
| `processors/` | 담당자가 구현할 metadata 분석기 |

`ANALYSIS_PLUGIN=패키지.모듈:팩토리`로 인자 없는 팩토리를 지정한다. 기본 `MetadataBlackBox`는 `unconfigured`를 반환한다. **[객체 처리 계약](../../../docs/SRS_interface_analysis.md)**에서 입력·반환값·실패 처리·배포 방법을 확인한다.

이 분석기는 `global_person_id`를 지정할 수 없다. 인물 연결 작업과 완료 순서는 독립적이다. 스냅샷·모델은 읽기 전용이며 DB는 직접 열지 않는다.

필수 설정은 `DATA_ANALYSIS_TOKEN`, 선택 설정은 `DATA_SERVICE_URL`, `SNAPSHOTS_ROOT`, `ANALYSIS_PLUGIN`이다. 모델 의존성은 이 서비스의 `requirements.txt`에 추가한다.

[개발 환경](../../../README.md#개발과-검증)을 준비한 뒤 저장소 루트에서:

```powershell
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml exec analysis python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/analysis/tests -q
```

실행은 중앙 Compose를 사용한다. 시간 초과·블랙박스 상태는 [운영 상태](../../../docs/architecture.md#상태-확인)를 따른다.
