# 관제 화면 이벤트 상세의 Analysis 결과 표시 계획

작성일: 2026-11-04, Asia/Seoul  
상태: 계획 수립, 구현 전

## 목표와 전제

데스크톱 관제 화면에서 **이벤트 목록 → 이벤트 클릭 → 이벤트 상세 → 분석 결과** 순서로 Analysis 결과를 확인한다. 사용자가 요청한 분석 상태·완료 시각, 상·하의 후보 영역의 대표색·신뢰 지표, 이미지 품질, 새로고침 버튼을 추가한다.

기존 `GET /api/v1/events/{event_id}`가 반환하는 `metadata.analysis`를 사용한다. 현재 기본 분석기 `local_appearance`의 결과 표시를 기준으로 계획한다. PAR 분석기의 성별·연령·소지품 등 별도 속성 표시는 이번 요청에 추가하지 않는다. 다른 분석기의 이벤트도 실패나 빈 결과로 오해하지 않도록 제공 여부를 설명한다.

## 확인한 현재 구조

| 위치 | 현재 동작과 변경 이유 |
| --- | --- |
| `server/desktop/admin/legacy_gui.py`의 `add_event()` | ID가 있는 이벤트를 클릭하면 `open_event_detail()`을 연다. 진입 경로는 이미 있다. |
| `server/desktop/admin/application.py`의 `open_event_detail()` | 이벤트 기본 정보·이미지·관련 녹화를 한 번 조회해 스크롤 영역에 표시한다. Analysis 결과 표시와 새로고침 버튼은 없다. |
| `server/desktop/admin/api.py`의 `DesktopApi.event()` | 인증된 이벤트 상세 API를 호출한다. 같은 메서드로 분석 결과를 다시 받을 수 있다. |
| `server/desktop/common/tasks.py`의 `BackgroundTask` | 네트워크 작업을 UI 스레드 밖에서 실행한다. 작업은 완료될 때까지 소유자가 보관해야 한다. |
| `server/services/data/app/database/repositories/objects.py` | 결과를 `metadata.analysis = {status, updated_at, result}`에 저장한다. |
| `server/services/analysis/processors/appearance.py` | 대표색은 `result.clothing_colors`, 품질은 `result.image.quality`에 있다. |
| `server/setup/install_helper/tests/test_desktop_event_detail_contract.py` | 이벤트·이미지·녹화 API 어댑터의 기존 계약 테스트가 있다. 상세 창 갱신 동작은 추가 검증이 필요하다. |

## 화면과 표시 규칙

기본 정보 바로 아래, 이미지보다 앞에 **분석 결과** 영역을 추가한다. 기존 스크롤 창을 사용하며 추가 내용을 세로로 읽을 수 있게 한다. 하단에는 **분석 결과 새로고침**, **닫기**를 둔다.

| 항목 | 데이터와 표시 방식 |
| --- | --- |
| 분석 상태 | `metadata.analysis.status`를 대기 중·처리 중·완료·실패·분석기 미설정으로 표시한다. |
| 시각 | `complete`이면 `updated_at`을 분석 완료 시각으로 표시한다. 그 밖에는 마지막 상태 갱신 시각으로 표시한다. 운영 PC의 현지 시각과 시간대를 함께 표기한다. |
| 상의·하의 후보 영역 | 각 `dominant_color.name`을 한국어 색명으로 표시하고 RGB와 `confidence`를 함께 적는다. 신뢰 지표는 소수로 표시한다. |
| 이미지 크기 | `image.width/height`와 `sample_width/sample_height`를 원본·측정 표본 크기로 표시한다. |
| 밝기·대비 | `luminance_mean`, `luminance_stddev`를 밝기 평균·표준편차로 표시한다. |
| 윤곽 에너지 | `laplacian_variance`를 표시한다. 낮다는 이유만으로 흐림을 확정하지 않는다. |
| 암부·명부 비율 | `dark_pixel_fraction`, `bright_pixel_fraction`을 픽셀 비율로 표시한다. |
| 품질 플래그 | `mostly_dark`, `mostly_bright`, `low_contrast`, `low_edge_energy`를 한국어로 풀어 쓴다. 빈 목록은 '해당 플래그 없음'으로 표시한다. |

분석 영역에는 '단일 사람 이미지의 후보 영역 측정값이며, 신뢰 지표는 정답 확률이 아닙니다'라는 짧은 설명을 둔다. 기존 이벤트의 `Confidence`는 **감지 Confidence**로 명확히 표시해 색상 신뢰 지표와 구분한다.

상태별 동작:

- `pending`·`running`: 대기·처리 상태와 새로고침 안내를 표시한다. 결과가 남아 있어도 현재 완료 결과로 표시하지 않는다.
- `complete`: 현재 분석기의 제공 필드만 표시한다. 없는 필드는 0으로 채우지 않고 '제공되지 않음'으로 표시한다.
- `failed`: 분석 실패와 서버가 제공하는 오류 코드를 표시한다. UI에서 원인을 임의 추정하지 않는다.
- `unconfigured`: 분석기 미설정으로 표시한다.
- Analysis 항목이 없는 이벤트: '이 이벤트에 저장된 분석 정보가 없습니다'로 표시한다. 화면만 보고 Analysis 서비스 중단으로 단정하지 않는다.
- 다른 backend: 분석 상태와 시각은 표시하고, 색상·품질 필드가 없으면 '이 분석기에서 제공되지 않음'으로 표시한다.
- 알 수 없는 상태·부분 누락 결과: '알 수 없음'·'제공되지 않음'으로 표시하고 기본 상세 내용은 계속 사용할 수 있게 한다.

## 구현 순서와 단계별 검증

### 1. Analysis 표시 영역 추가

`application.py`의 기존 상세 창에 분석 영역을 추가한다. 상태·색명·품질 플래그의 표시 규칙을 작은 함수 또는 상수로 정리하고, 현재 창 안에서 필요한 범위로 한정한다. 서버가 반환한 문자열은 기존 방식처럼 일반 텍스트로 표시한다.

반복 갱신 시 분석 영역의 기존 내용을 교체한다. 기본 정보·이미지·녹화 위젯을 매번 추가하거나 같은 위젯을 중복 생성하지 않는다.

검증: 완료·대기·실패·미설정·분석 정보 없음·다른 backend의 표본 응답에서 표시 규칙이 맞고, 색상 신뢰 지표와 감지 Confidence가 구분된다.

### 2. 수동 새로고침 연결

버튼을 누르면 `BackgroundTask`에서 같은 이벤트 ID로 `DesktopApi.event()`만 다시 호출한다. 갱신 대상은 분석 영역이다. 사진과 녹화를 재다운로드하지 않는다.

처음 상세를 불러오는 동안과 새로고침 작업 중에는 버튼을 비활성화한다. 한 창에서 동시에 하나의 조회만 수행하고 완료·실패 후 버튼을 복구한다. 대기 중인 분석이 완료되어도 자동 폴링 없이 사용자가 버튼으로 갱신한다.

새로고침 실패 시 이전에 성공한 표시를 보존하고 '새로고침 실패, 이전 조회 결과 표시 중'과 기존 `safe_error()`로 정리한 오류를 안내한다. 첫 조회 실패는 기존 로딩·실패 안내 경로를 유지한다.

검증: `pending → running → complete` 순서의 응답을 버튼으로 반영하고, 연속 클릭이 중복 요청을 만들지 않으며, 실패 후 재조회가 가능하다. 새로고침이 이미지·녹화 API를 추가 호출하지 않는다.

### 3. 창 수명과 비동기 응답 처리

기존 `_event_detail_tasks`를 사용해 작업을 완료까지 보관한다. 창을 닫는 즉시 해당 창의 결과·오류 반영을 중단하고, 작업 종료 시 참조를 정리한다. 파괴 시점뿐 아니라 대화상자 종료도 닫힘으로 처리한다.

초기 조회와 새로고침은 동시에 실행하지 않아 이전 응답이 새 결과를 덮어쓰지 않게 한다. 네트워크 작업 중에도 UI 이벤트 루프와 닫기 동작을 유지한다.

검증: 조회 중 창 닫기, 닫은 뒤 늦은 응답 도착, 같은 이벤트 재열기에서 Qt 위젯 접근 오류와 실행 중 QThread 파괴가 발생하지 않는다.

### 4. 회귀·화면 검증과 안내 갱신

기존 테스트 방식인 `unittest`와 Qt offscreen 환경을 사용한다. 새 GUI 동작 테스트는 `server/setup/install_helper/tests/test_desktop_event_analysis.py`에 둔다. 목 API와 완료 시점을 조절하는 작업을 사용해 상태 전환·중복 요청 방지·실패 후 복구·창 닫기를 검증한다.

구현 후 다음 검사를 수행한다.

```powershell
uv run --project server/setup/install_helper --locked python -m unittest discover -s server/setup/install_helper/tests -p 'test_desktop_event_analysis.py'
uv run --project server/setup/install_helper --locked python -m unittest discover -s server/setup/install_helper/tests -p 'test_desktop_event_detail_contract.py'
uv run --project server/setup/install_helper --locked python -m unittest discover -s server/setup/install_helper/tests -p 'test_multi_camera_view.py'
git diff --check
```

실제 관제 화면에서는 저장된 완료 이벤트를 열어 API 값과 대조한다. 560×640 기본 크기와 480×360 최소 크기에서 줄바꿈·스크롤·버튼 접근성을 확인한다. 스냅샷·사람 크롭·기존 박스 이미지·관련 녹화 재생이 유지되는지도 확인한다. 목 응답 테스트의 상태 전환 성공과 실제 운영 결과 표시 성공은 구분해 기록한다.

사용 경로와 수동 새로고침 방법을 `server/desktop/admin/README.md`에 짧게 추가한다.

## 예정 변경 파일과 완료 기준

| 파일 | 예정 변경 |
| --- | --- |
| `server/desktop/admin/application.py` | 분석 영역, 표시 규칙, 수동 갱신, 관련 작업 수명 처리 |
| `server/setup/install_helper/tests/test_desktop_event_analysis.py` | 상태·갱신·실패·창 종료 동작 테스트 추가 |
| `server/desktop/admin/README.md` | 이벤트 상세에서 분석 결과를 확인하는 방법 안내 |

완료 기준은 사용자가 API나 DB 도구 없이 이벤트 상세에서 요청한 분석 항목을 읽고, 새로고침으로 최신 결과를 확인하는 것이다. 반복 조회 시 내용이 쌓이지 않고, 오류와 누락 필드에서도 상세 창을 사용할 수 있어야 한다. 화면 표시에는 기존 API를 사용하며 서버·DB·분석 플러그인의 변경은 필요하지 않다.

이번 산출물은 계획 문서다. 기능 구현과 테스트 실행은 아직 수행하지 않았다.
