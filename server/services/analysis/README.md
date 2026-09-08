# Analysis

Data에서 작업과 사람 영역 이미지(크롭)의 경로를 받아 추가 정보(`metadata`)를 돌려주는 독립 컨테이너다. Preprocessing과 직접 통신하지 않는다.

| 위치 | 수정할 내용 |
|---|---|
| `app/` | 작업기 시작·종료, Data 연결, 상태 확인 |
| `processors/` | 담당자가 구현할 metadata 분석기 |

기본 `MetadataBlackBox`는 구현 자리만 제공하는 블랙박스다. `unconfigured`를 반환하며 실제 속성 분석을 수행하지 않는다. `ANALYSIS_PLUGIN=패키지.모듈:팩토리`로 인자 없는 생성 함수를 지정해 구현을 교체한다. **[교체 규약](../../../docs/SRS_interface_analysis.md)**에서 입력·반환값·실패 처리·배포 방법을 확인한다.

이 분석기는 `global_person_id`를 지정할 수 없다. 인물 연결 작업과 완료 순서는 독립적이다. 스냅샷·모델은 읽기 전용이며 DB는 직접 열지 않는다.

## 실행과 검증

중앙 Compose로 실행한다. 플러그인 선택은 `server/.env`의 `ANALYSIS_PLUGIN`, Data 인증키는 `server/secrets/analysis.env`의 `DATA_ANALYSIS_TOKEN`이 기본 위치다. 모델 의존성은 이 서비스의 `requirements.txt`에 추가한다.

[개발 환경](../../../README.md#서버-코드-개발)을 준비하고 개발 Compose를 기동한 뒤, 저장소 루트에서 실행한다. 아래 `server/.env`는 개발 전용 설정이다.

```powershell
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml exec analysis python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/analysis/tests -q
```

시간 초과·블랙박스 상태는 [상태 확인](../../../docs/architecture.md#상태-확인)을 따른다.
