# Analysis 서비스

Data에서 작업과 사람 영역 이미지(크롭)의 경로를 받아 추가 정보(`metadata`)를 돌려주는 독립 컨테이너다. Preprocessing과 직접 통신하지 않는다.

| 위치 | 수정할 내용 |
|---|---|
| `app/` | 작업기 시작·종료, Data 연결, 상태 확인 |
| `processors/appearance.py` | 기본 CPU 색상·영상 품질 분석기 |
| `processors/__init__.py` | 기본 분석기 export와 이전 `MetadataBlackBox` 호환 |
| [`lib/ai_cctv_core/processing/`](../../../lib/ai_cctv_core/processing/) | 인물 연결과 분석이 공유하는 작업 임대·입력 검증·시간 제한·결과 보고 |

기본 `LocalAppearanceAnalyzer`는 OpenCV·NumPy로 단일 사람 크롭의 상·하의 후보 영역 색과 영상 품질을 실제 측정한다. 외부 모델 파일이나 다운로드가 필요 없는 CPU 구현이다. `ANALYSIS_PLUGIN=server.services.analysis.processors:LocalAppearanceAnalyzer`가 기본이며, `패키지.모듈:팩토리` 형태의 인자 없는 생성 함수로 교체할 수 있다. 이전 `MetadataBlackBox`를 명시적으로 선택하면 종전처럼 `unconfigured`를 반환한다.

이 분석기는 `global_person_id`를 지정할 수 없다. 인물 연결 작업과 완료 순서는 독립적이다. 스냅샷·모델은 읽기 전용이며 DB는 직접 열지 않는다.

## 현행 입출력과 구현 기준

플러그인은 `process(job: dict, crop_path: Path) -> dict`를 제공한다. `job`은 Data에서 임대한 객체 작업이며 `crop_path`는 검증된 로컬 크롭 경로다. 기본 분석기는 [공용 JPEG 로더](../../../lib/ai_cctv_core/processing/images.py)로 8 MiB 이하·1,600만 픽셀 이하, 관측 bbox와 정확히 일치하는 크기의 JPEG만 읽는다. 각 변이 16픽셀 미만이거나 축소 후 너무 좁은 크롭은 거부한다. 반환값은 `lease_id`를 제외한 `ObjectJobCompletion` 필드이고 공통 실행기가 임대 ID를 붙여 완료 결과를 보고한다. 교체 플러그인도 동일한 입력 검증을 유지해야 한다.

`outcome`은 `complete`·`retry`·`failed`·`unconfigured` 중 하나다. 추가 정보는 최대 64 KiB의 유한 JSON 객체 `metadata`에 담으며 Analysis 결과의 `global_person_id`와 `identity_descriptor`는 지정하지 않는다. 현재 호출 형식은 [플러그인 프로토콜](../../../lib/ai_cctv_core/processing/plugins.py), 필드 검증은 [객체 스키마](../../../lib/ai_cctv_core/contracts/objects.py), 오류·시간 제한 처리는 [공통 실행기](../../../lib/ai_cctv_core/processing/worker.py)와 [작업기 수명 관리](../../../lib/ai_cctv_core/processing/runtime.py)를 따른다.

### 기본 분석 결과의 의미

`metadata.backend`는 `local_appearance`, `backend_version`은 `1.0.0`, `schema_version`은 `1`이다. `image`에는 원본·표본 크기와 밝기 평균·표준편차, 검거나 밝게 포화된 픽셀 비율, Laplacian 분산을 담는다. 연산 표본은 긴 변 최대 256픽셀이다.

`clothing_colors.upper`와 `lower`는 얼굴을 피한 중앙 상·하체의 기하학적 영역에서 구한 대표색이다. HSV 밝기·채도로 흑백·회색을 구분하고 유채색을 묶는다. 양옆과 중앙 색 분포가 다를 때만 양옆 색의 가중치를 낮춘다. 각 영역의 `dominant_color`는 색명, 중앙값 RGB, 원래 픽셀 비율 `fraction`, 가중 비율 `weighted_fraction`, 색상 일관성 지표 `confidence`를 포함한다. 상위 세 색의 `palette`, 실제 표본 영역과 픽셀 수도 제공한다.

이 결과는 의복 분할·의복 종류·행동 인식 결과가 아니다. 자세·가림·배경·조명에 따라 다른 픽셀이 섞일 수 있고, 배경과 옷이 같은 색이면 분리할 수 없다. `confidence`는 정답 확률이 아니며 `low_edge_energy`도 흐림을 확정하지 않는다. 근거와 한계는 `evidence`에 함께 기록한다. 나이·성별·인종이나 보이지 않는 속성은 추정하지 않는다.

[Analysis 계약 테스트](tests/test_analysis_contract.py)와 [객체 처리 통합 테스트](../../../tests/automated/test_object_processing.py)에서 현재 결과·권한·작업 임대 동작을 검증한다. Data와 통신할 때는 [객체 작업 API](../data/app/api/objects.py)와 [권한 검사](../data/app/security.py)를 유지한다.

`unconfigured`로 끝난 기존 작업은 모델 연결·컨테이너 재시작만으로 다시 처리하지 않는다. 모델을 연결하고 크롭 파일이 남아 있는지 확인한 뒤 내부 네트워크에서 `POST http://nginx:8080/internal/data/v1/object-jobs/analysis/requeue-unconfigured`를 호출한다. 본문은 없고 `X-Internal-Token`에는 `DATA_ANALYSIS_TOKEN`을 사용한다. 응답 `{"requeued": 수량}`만큼 최대 100건씩 다시 대기하므로 남은 작업이 있으면 반복한다. 완료·재시도·재등록의 저장 규칙은 [Data 작업 저장소](../data/app/database/repositories/objects.py)를 따른다.

## 실행과 검증

중앙 Compose로 실행한다. 플러그인 선택은 `server/.env`의 `ANALYSIS_PLUGIN`, Data 인증키는 `server/secrets/analysis.env`의 `DATA_ANALYSIS_TOKEN`이 기본 위치다. 이 토큰은 32자 이상이며 Data에 설정한 같은 이름의 토큰과 일치해야 한다. 모델 의존성은 이 서비스의 `requirements.txt`에 추가하고 이미지를 다시 빌드한다. 모델은 `MODELS_DIR`에서 컨테이너의 `/models`에 읽기 전용으로 연결되며, 사용할 파일은 플러그인이 선택한다.

[개발 환경](../../../docs/guide.md#서버-코드-개발)을 준비하고 개발 Compose를 기동한 뒤, 저장소 루트에서 실행한다. 아래 `server/.env`는 개발 전용 설정이다.

```powershell
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml exec analysis python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/analysis/tests -q
```

플러그인 초기화·호출은 별도 `spawn` 자식 프로세스에서 실행한다. `OBJECT_MODEL_TIMEOUT_SECONDS`는 기본 120초(`0 < 값 ≤ 240`), `OBJECT_STARTUP_TIMEOUT_SECONDS`는 기본 30초(`0 < 값 ≤ 120`)다. 시간 초과한 자식은 종료한 뒤 재생성하며 초기화 오류는 30초 후 재시도한다. 완료 HTTP 응답이 유실되면 보관한 동일 lease·본문을 재전송해 같은 프로세스 안에서 모델을 다시 실행하지 않는다. 프로세스 전체가 중단되면 Data의 임대 만료 후 재처리될 수 있다.

`/health/live`는 프로세스 생존 여부, `/health/ready`는 작업 처리 통로의 상태다. `backend`·`model_ready`·`last_error`·`last_outcome`을 실제 결과와 함께 확인한다. 명시적으로 선택한 블랙박스는 `unconfigured`이며 HTTP 200이 모델 성능 검증을 뜻하지 않는다. 세부 응답은 [상태 확인](../../../docs/architecture.md#상태-확인)을 따른다.

[기본 분석기 테스트](tests/test_local_appearance.py)는 실제 합성 JPEG로 상·하체 색 분리, 배경 억제, 흑백·다색·노출 상태, 크기 제한과 손상 입력 거부를 확인한다. 로컬 Python 환경에서 다음으로 재현할 수 있다.

```powershell
python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/analysis/tests -q
```

이는 카메라·Docker 없이 수행하는 코드 및 JPEG 처리 검증이다. 실제 CCTV 의복 정답률과 운영 컨테이너 기동·처리시간은 검증하지 않았다. 개발 Compose에서 실제 크롭을 등록하고 `metadata.analysis.result`의 영역·RGB·비율을 원본과 비교하는 절차는 [Analysis 인수 문서](../../../docs/SRS_interface_analysis.md#5-인수-검증)를 따른다.
