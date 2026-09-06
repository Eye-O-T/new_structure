# Analysis

객체의 metadata 분석을 담당하는 독립 컨테이너입니다. 모델 선택·특성 추출은
담당 개발자가 구현하며, 이 서비스는 작업 수신·결과 검증·반환·상태 확인을 제공합니다.

## 교체 지점

`processors/MetadataBlackBox`는 분석하지 않고 아래 결과만 반환합니다.
가짜 속성·분석 점수·전역 인물 ID는 만들지 않습니다.

```json
{"outcome":"unconfigured","metadata":{"reason":"metadata_backend_not_implemented"}}
```

`ANALYSIS_PLUGIN=패키지.모듈:팩토리`로 인자 없는 팩토리를 지정하십시오.
기본값은 `server.services.analysis.processors:MetadataBlackBox`입니다.

```python
class MyAnalyzer:
    def process(self, job: dict, crop_path: Path) -> dict:
        # 실제 모델로 crop_path의 객체를 분석합니다.
        # {"outcome": "complete", "metadata": 실제_분석결과}를 반환합니다.
        ...
```

## 입력·출력 계약

공통 스키마는 `ai_cctv_core.contracts.objects`에 있습니다. `job`에는 `id`,
`event_id`, `camera_id`, `person_id`, `lease_id`, `object_observation`이 포함됩니다.
`object_observation.schema_version=1`이며, 추적 세션 ID, 원본 픽셀 기준
`[x1,y1,x2,y2]` 박스, 프레임 크기와 크롭의 상대 경로를 포함합니다.
`crop_path`는 `/snapshots` 내부의 존재하는 파일로 검증된 절대 경로입니다.
원본 이미지 파일은 수정하지 않습니다.

`person_id`는 카메라·추적 세션 범위의 ID입니다. 카메라 간 동일인 연결은
Preprocessing의 인물 연결 처리기 담당입니다. 분석기는 `global_person_id`를
할당할 수 없으며 반환하면 실패 결과로 처리됩니다. 인물 연결과 분석 작업의
완료 순서는 보장되지 않으므로 전역 ID가 이미 존재한다고 가정하지 마십시오.

반환값은 `outcome`과 JSON 객체 `metadata`입니다. `outcome`은 `complete`,
`retry`, `failed`, `unconfigured` 중 하나입니다. metadata는 최대 64 KiB이고
NaN·Infinity는 허용하지 않습니다. `lease_id`는 실행기가 추가합니다.
Data는 결과를 해당 이벤트의 `metadata.analysis`에 병합하며 인물 연결 결과와
원본 객체 관측 정보를 보존합니다. 분석기의 내부 속성 스키마는 담당 개발자가
정의하되 모델명·결과 스키마 버전을 metadata에 포함하는 것을 권합니다.

## 작업 전달과 실패 처리

- `POST /object-jobs/analysis/claim`: 임대가 설정된 작업 한 건 수신
- `POST /object-jobs/analysis/{id}/complete`: 검증된 결과 반환
- `POST /object-jobs/analysis/requeue-unconfigured`: 모델 배포 후 미구현 작업 재처리

경로는 `DATA_SERVICE_URL`에 대한 상대 경로입니다. 모든 요청은
`DATA_ANALYSIS_TOKEN`을 `X-Internal-Token` 헤더로 전달합니다. 작업은 장애 복구
과정에서 중복 실행될 수 있고, 만료된 임대 결과는 Data가 무시합니다. 외부 저장소
쓰기 등 부수 효과가 있으면 작업 ID를 기준으로 중복 처리를 막아야 합니다.

실행기는 모델 예외를 `retry`, 잘못된 출력·파일 경로를 `failed`로 반환합니다.
모델 제한 시간은 120초입니다. 시간 초과된 동기 호출을 강제로 종료할 수 없어
추가 모델 호출은 중단하고 준비 상태를 실패로 표시합니다. 구현체에서 자체
시간 제한·종료 처리를 제공하고 멈춘 작업기는 컨테이너를 재시작하십시오.

## 실행과 검증

필수 환경변수는 `DATA_ANALYSIS_TOKEN`(32자 이상), 선택 설정은
`DATA_SERVICE_URL`, `SNAPSHOTS_ROOT`, `ANALYSIS_PLUGIN`입니다. DB 접근과
외부 공개 포트는 필요하지 않습니다. 스냅샷·모델 저장소는 읽기 전용입니다.

`/health/live`는 프로세스 생존 여부, `/health/ready`는 작업기와 Data 통신 상태를
반환합니다. `last_outcome=unconfigured`는 분석 알고리즘이 아직 없음을 뜻하며
서비스 통신 준비 여부와 별개입니다.

저장소 루트에서 `python -m pytest server/services/analysis/tests`로 모델 없이
계약·블랙박스 동작·인물 ID 할당 금지를 검증합니다. 실제 분석 모델의 정확도는
담당 개발자의 별도 검증 대상입니다. 기본 실행은 Compose를 사용하며 직접
실행 시 저장소 루트와 `src/`를 `PYTHONPATH`에 포함하고
`uvicorn server.services.analysis.app.main:app`을 실행합니다.
