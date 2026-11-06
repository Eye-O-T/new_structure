# 관제 화면 표시 문제 조사 및 수정계획서

- 작성일: 2026-11-06, 한국시간(KST)
- 조사 기준: 저장소 `dd9d02d`와 조사 시 읽을 수 있는 설치 설정·DB
- 대상: Windows PyQt 관제 화면 `server.desktop.admin.application.CCTVMainWindow`
- 작업 상태: 계획에 따른 관제 화면 변경을 구현하고 가능한 검사를 수행했다. 아래에 검사 범위와 남은 실제 GUI 검증을 기록한다.
- 사용자 확인 반영: `person_id` 미표시는 단순 시간 지연이었다. 화면 표기는 `pid`·`gpid`로 통일하고, 전역 ID 미판정 시 `gpid:null`로 표시한다.

## 1. 요구사항과 판정

| 번호 | 요구사항 | 조사 결과 |
| --- | --- | --- |
| 1 | 사람 박스에 `pid`와 `gpid` 모두 표시, 전역 ID 미판정 시 `gpid:null` 표시 | 사용자 확인에 따라 기존 ID 미표시는 시간 지연으로 판정한다. 두 ID 중 하나만 선택하는 현행 라벨은 두 값을 함께 표시하도록 변경한다. |
| 2 | 이벤트를 위에서 아래로 자연스럽게 읽기 | 최신 이벤트를 맨 위에 넣는 동작을 현행 함수 실행으로 재현했다. |
| 3 | 이벤트 시간을 한국시간으로 표시 | UTC 문자열을 변환 없이 카드와 상세 화면에 넣는 코드를 확인하고 카드 경로에서 재현했다. |

2번은 **위쪽이 오래된 이벤트, 아래쪽이 최신 이벤트이며 신규 이벤트는 아래에 추가**되는 요구로 해석한다. 최근 30건 보관과 카메라별 조회는 유지한다. 1번은 실시간 영상 위의 박스·라벨을 대상으로 한다. 저장된 분석 이미지 자체에 전역 ID를 다시 합성하는 작업은 포함하지 않는다.

## 2. 확인 범위와 제약

소스, 설치 설정 `C:\ProgramData\AI_CCTV\config\config.yaml`·`compose.env`, 설치 DB `C:\ProgramData\AI_CCTV\database\ai_cctv.db`의 관련 테이블을 확인했다. 비밀번호·인증 토큰·영상·인물 특징 벡터는 조사 결과에 포함하지 않았다.

DB는 파일 바이트를 메모리로 읽어 조회했다. 당시 WAL 보조 파일이 없음을 확인했고, 메모리 사본에만 저널 모드 헤더를 조정한 뒤 `query_only`로 조회했다. 조회 전후 원본 내용이 동일함을 확인했다. 이는 저장된 상태의 근거이며 실행 중 서버의 API 응답과 동일하다는 보장은 없다.

현재 도구 환경에는 Docker CLI가 없고, 프로젝트 가상환경이 가리키는 Python 3.11 실행 파일도 없다. 사용 가능한 Python에는 PyQt5·av·pydantic 등이 없다. 따라서 Docker 로그·실제 모델 추론·실제 GUI 재현은 수행하지 못했다. PyQt 관련 확인은 소스에서 해당 함수를 추출하여 위젯·페인터 대역과 실행한 결과이며, 전체 애플리케이션 통합 테스트와 구분한다.

## 3. 문제별 원인

### 3.1 인물 ID 표시 지연과 pid·gpid 표기

사용자가 `person_id` 미표시 원인을 단순 시간 지연으로 확인했다. 이를 신고 원인에 대한 최종 판정으로 반영하고, 원인 미확정에 따른 추가 장애 조사는 계획에서 제외한다. 앞으로 화면에서 `person_id`는 `pid`, `global_person_id`는 `gpid`로 표시한다. API·DB 필드명은 유지한다.

#### A. 변경할 현행 표시 방식: 두 ID 중 하나만 선택

현행 카메라 타일은 아래 식의 결과 하나만 라벨로 그린다.

```python
item.get("global_person_id") or item.get("person_id") or "person"
```

- [camera_view.py:257](../server/desktop/admin/camera_view.py#L257): 박스와 라벨 표시 조건 및 라벨 선택.
- [legacy_gui.py:439](../server/desktop/admin/legacy_gui.py#L439): 이전 표시 경로에도 같은 선택식이 있다.
- [video_worker.py:117](../server/desktop/admin/video_worker.py#L117): 이벤트 어댑터도 전역 ID가 있으면 `person_id` 필드에 전역 ID를 대신 넣는다.

`person_id="7", global_person_id=42` 입력으로 실제 `_render()` 함수를 페인터 대역과 실행한 결과, 박스는 그리지만 문자열은 `42` 하나였다. 전역 ID가 없으면 `7`로 바뀌었다. 이 동작은 신고된 시간 지연과 별도로, 두 ID를 함께 표시하는 새 요구사항에 맞게 변경한다.

#### B. 이전 조사 참고 자료: 좌표 표시 경로와 저장 상태

아래는 이전 조사에서 확인한 코드와 저장 데이터다. 사용자 확인으로 신고 원인은 시간 지연으로 정정되었으므로, 이 저장 상태를 신고 당시 장애 원인으로 채택하지 않는다.

실시간 영상은 원본 스트림이며, 박스는 별도 좌표 API를 통해 그린다.

```text
MediaMTX 영상 → Preprocessing 감지 → Data 최신 좌표 저장
                                  → Data가 세션별 전역 ID 결합
                                  → External /api/v1/cameras/{camera_id}/objects
                                  → VideoWorker → CameraView
```

전처리 컨테이너가 실행 중인 것만으로 카메라별 감지·좌표 전달·표시가 모두 정상이라는 뜻은 아니다.

| 단계 | 확인된 조건·근거 | 표시가 사라지는 경우 |
| --- | --- | --- |
| 감지 대상 선정 | [Data cameras.py:93](../server/services/data/app/api/cameras.py#L93), [supervisor.py](../server/services/preprocessing/app/supervisor.py) | 활성 카메라라도 `stream_requested=false`이면 감지 대상 목록에서 제외된다. |
| 화면 작업자 시작·수신 | [application.py:484](../server/desktop/admin/application.py#L484), [application.py:584](../server/desktop/admin/application.py#L584) | `recording_enabled=false`이면 작업자를 시작하지 않거나 수신 데이터를 받지 않는다. 로컬 카메라는 미리보기 요청도 필요하다. |
| 좌표 저장·조회 | [objects.py:34](../server/services/data/app/database/repositories/objects.py#L34) | 좌표가 없거나 관측 시각이 3초 초과·미래이면 `stale=true`, `objects=[]`를 반환한다. |
| 좌표 통신 | [video_worker.py:203](../server/desktop/admin/video_worker.py#L203) | 조회 실패 시 오래된 박스를 지우는 응답을 화면에 전달한다. |
| 화면 표시 | [camera_view.py:220](../server/desktop/admin/camera_view.py#L220), [overlay.py:32](../server/desktop/admin/overlay.py#L32) | 표시 설정 OFF, stale 응답, 유효하지 않은 크기·좌표, 수신 후 3초 초과이면 그리지 않는다. |

설치 DB에서 확인한 상태는 다음과 같다. 아래 시각은 원본 UTC를 한국시간으로 변환한 값이다.

| 항목 | 저장된 값 |
| --- | --- |
| 카메라 | `local-camera`, `enabled=1`, 저장된 `status=online` |
| 실행 요청 | `recording_enabled=0`, `stream_requested=0` |
| 마지막 좌표 | `2026-11-04 14:15:11.902 KST`, 1280×720, 객체 1개 |
| 좌표 내용 | `bbox=[184,206,1074,716]`, `person_id="1"`, 같은 세션의 `global_person_id=6` |
| 마지막 추론 스트림 손실 이벤트 | `2026-11-04 14:15:12.258 KST` |
| 사람 이벤트 | 등장 16건·퇴장 10건 모두 로컬 ID와 전역 ID 보유 |
| 객체 작업 | identity 16건·analysis 16건 모두 `complete` |

같은 메모리 DB에 현행 `get_live_objects()`를 실행하면 조사 시점에는 `stale=true`, `stale_reason=expired`, `objects=[]`가 된다. 시계 대역을 마지막 관측 후 1초로 설정하면 같은 코드가 박스·`person_id="1"`·`global_person_id=6`을 반환한다. 저장된 상태에서는 좌표 생성·전역 ID 연결 이력이 있고, 이후 신선한 관측이 끊긴 경로가 확인된다. 저장된 `online` 값만으로 현재 영상·추론이 정상이라고 판단해서는 안 된다.

**판정:** 신고된 `person_id` 미표시 원인은 사용자 확인에 따른 시간 지연이다. 저장된 송출·녹화 요청 OFF와 오래된 좌표는 과거 조사 자료로만 남긴다. 이번 수정은 지연 원인 재조사보다 `pid`·`gpid` 동시 표시와 `gpid:null` 표기에 집중한다.

#### C. 전역 ID는 비동기 결과

[pipeline.py:641](../server/services/preprocessing/app/pipeline.py#L641)는 박스와 로컬 ID를 먼저 전달한다. Data는 [objects.py:91](../server/services/data/app/database/repositories/objects.py#L91)의 연결 조회로 전역 ID를 붙인다. 전역 ID가 아직 없으면 박스와 `pid`를 유지하면서 `gpid:null`을 표시한다. 판정 후 다음 좌표 응답부터 실제 `gpid` 값으로 갱신한다. identity 모델 실패도 감지와 별도 경로이므로, `global_person_id=null`을 이유로 박스 전체를 숨겨서는 안 된다.

### 3.2 이벤트가 아래에서 위로 쌓임

최신 이벤트가 위에 오는 동작은 서버 응답 순서만의 문제가 아니다. 클라이언트가 다음 처리를 결합한다.

1. [application.py:647](../server/desktop/admin/application.py#L647): 카메라별 목록 선두 삽입 후 발생 시각 내림차순 정렬, 최근 30건 유지.
2. [application.py:716](../server/desktop/admin/application.py#L716): 목록을 역순으로 순회하여 카드 생성.
3. [legacy_gui.py:569](../server/desktop/admin/legacy_gui.py#L569): 각 카드를 다시 레이아웃 인덱스 `0`에 삽입.

현행 `_on_event()` → `_render_selected_summary()` → `add_event()`를 위젯 대역과 실행했다. 05:00, 05:01 UTC 순서로 이벤트를 보내면 화면 위→아래는 **05:01, 05:00**으로 나타났다. 같은 갱신마다 카드 전체를 재생성하므로 스크롤 위치 처리도 함께 검증해야 한다.

초기 최근 30건 조회는 `order=desc`, 이후 신규 수집은 `after_event_id`를 쓴다([video_worker.py:233](../server/desktop/admin/video_worker.py#L233)). 늦게 저장된 이벤트도 수집하기 위한 구조이므로 화면 순서를 바꾸려고 이 수집 기준까지 변경하지 않는다.

### 3.3 이벤트 시간이 한국시간으로 표시되지 않음

- [video_worker.py:126](../server/desktop/admin/video_worker.py#L126): `occurred_at`을 그대로 카드용 `time`에 복사한다.
- [legacy_gui.py:510](../server/desktop/admin/legacy_gui.py#L510): `time` 문자열을 `QLabel`에 바로 넣는다.
- [application.py:1383](../server/desktop/admin/application.py#L1383): 이벤트 상세의 발생 시각도 원본 문자열을 표시한다.
- [application.py:1257](../server/desktop/admin/application.py#L1257): 분석 갱신 시각은 인자 없는 `astimezone()`을 사용하여 PC 시간대에 의존한다.
- [application.py:1408](../server/desktop/admin/application.py#L1408): 이벤트에 연결된 녹화 시작·종료 시각도 원본 값을 표시한다.

설치 설정에는 이미 `server.timezone: Asia/Seoul`이 있으나, 위 표시 경로에서는 이 설정을 적용하지 않는다. [lib/time.py](../lib/ai_cctv_core/time.py)와 Compose는 UTC 저장·통신을 사용한다. 저장·통신의 UTC 사용은 정상이고 화면에서 한국시간으로 변환하는 단계가 빠진 것이 원인이다.

예를 들어 DB 이벤트 33의 `2026-11-04T05:14:54.226Z`는 화면에서 **`2026-11-04 14:14:54 KST`**로 표시되어야 한다.

## 4. 수정계획

### 1단계: 시간 지연을 반영한 표시 흐름 확인

- 기존 `person_id` 미표시는 시간 지연으로 확정하고, 이를 해결하기 위한 별도 장애 조사·설정 변경·성능 개선은 이번 범위에서 제외한다.
- 박스와 로컬 ID가 도착하면 즉시 `pid`를 표시하고, 전역 ID 판정을 기다리며 박스나 `pid` 표시를 보류하지 않는다.
- 전역 ID가 아직 판정되지 않았으면 `gpid:null`로 표시하고, 판정 후 다음 정상 좌표 응답에서 실제 값으로 갱신한다.
- 기존 좌표 만료 기준, 카메라 시작 조건, 모델·송출 설정은 유지한다.

검증 완료 조건: 전역 ID 판정 전에도 박스와 `pid`가 표시되고, `gpid:null`이 판정 후 실제 값으로 바뀐다.

### 2단계: 두 ID를 명확히 표시

- `overlay.py`에 작은 순수 라벨 함수를 두고 실제 카메라 타일과 기존 렌더 경로가 함께 사용한다.
- 화면 라벨에서 `person_id`는 소문자 **`pid`**, `global_person_id`는 소문자 **`gpid`**로 표기하고 두 항목을 항상 함께 표시한다.
- 판정된 예시는 **`pid:7 gpid:42`**, 전역 ID 미판정 예시는 **`pid:7 gpid:null`**이다. 화면의 미판정 값은 소문자 문자열 `null`로 표시한다. API·DB의 실제 null 값과 기존 필드명은 유지한다.
- 라벨에 배경과 적절한 여백을 주고 영상 경계 안으로 위치를 조정하여 밝은 영상·화면 가장자리에서도 읽히게 한다.
- `legacy_event()`는 원래 `person_id`를 보존한다. 이벤트 카드에 ID를 표시할 때도 로컬·전역 ID 의미가 섞이지 않게 같은 표기 규칙을 적용한다.
- identity 완료 후 다음 좌표 응답에서 전역 ID가 갱신되는지 확인한다. 기존 카메라·추적 세션별 연결 규칙을 사용한다.

검증 완료 조건: 두 ID가 있으면 `pid:7 gpid:42`, 전역 ID 미판정 시에는 `pid:7 gpid:null`로 표시된다. 두 경우 모두 박스와 `pid`가 유지되고, 세션 재시작 전의 전역 ID가 새 추적에 잘못 붙지 않는다.

### 3단계: 타임라인을 오래된 순서로 표시

- 내부 최근 30건 보관과 신규 수집 watermark는 유지한다. 초기 API 조회를 오름차순으로 바꿔 가장 오래된 30건을 가져오는 실수를 피한다.
- 현재 `reversed(events)`가 전달하는 오래된 순서를 그대로 화면에 배치하도록 `add_event()`의 선두 삽입을 하단 추가로 변경한다.
- 카드 자체의 30건 제한도 초과 시 맨 위의 가장 오래된 카드를 제거하도록 맞춘다. 저장 목록 제한과 화면 제한을 함께 검증한다.
- 같은 발생 시각의 이벤트는 `id`로 순서를 정해 재표시 때 위치가 흔들리지 않게 한다. 늦게 저장된 과거 이벤트는 발생 시각에 맞는 위치에 들어간다.
- 스크롤 영역을 인스턴스에서 참조할 수 있게 하고, 최신 위치를 보고 있던 경우에만 갱신 뒤 하단을 따라간다. 사용자가 과거 카드를 읽고 있으면 가능한 한 현재 카드·스크롤 위치를 유지한다.
- 카메라를 바꿀 때는 선택한 카메라의 목록이 실제 표시되도록 렌더 캐시와 스크롤 상태를 검증한다. 목록·재표시 구조의 전면 교체는 하지 않는다.

검증 완료 조건: 초기 조회·연속 수신·늦은 이벤트·31건 이상·카메라 전환에서 모두 위→아래로 시간이 증가한다. 최신 이벤트를 놓치지 않으면서 과거 내용을 읽는 사용자의 위치를 불필요하게 이동시키지 않는다.

### 4단계: 이벤트 관련 시각을 KST로 통일

- 관제 화면에 작은 시간 표시 함수를 추가한다. 시간대가 있는 ISO 8601을 파싱하여 **한국시간 UTC+09:00**으로 변환한다. 현 요구사항은 한국시간 고정이므로 표준 라이브러리 `timezone(timedelta(hours=9), name="KST")`를 사용하여 Windows의 별도 시간대 데이터 설치에 의존하지 않는다.
- 카드와 상세 발생 시각은 `YYYY-MM-DD HH:mm:ss KST`로 표시한다. 분석 상태 갱신 및 연결 녹화의 시작·종료 시각도 같은 함수를 사용한다.
- API 시각과 정렬용 원본 값은 보존하고 화면 출력에서만 변환한다. PC 시간대가 UTC여도 한국시간이 표시되어야 한다.
- 누락·잘못된 형식·시간대 없는 값은 `시각 확인 불가` 등 명시적 상태로 표시한다. 현재 시각으로 대체하거나 임의로 UTC로 간주하지 않는다. 표시할 시각이 없는 로컬 오류 이벤트는 정렬 시 별도 처리하여 유효한 서버 이벤트 순서를 깨뜨리지 않는다.

검증 완료 조건: `Z`, `+00:00`, `+09:00`으로 표현된 동일 시점이 같은 KST로 표시된다. 날짜가 바뀌는 변환에서도 날짜가 정확하고, 저장값·조회 범위·정렬 기준은 유지된다.

## 5. 예상 변경 파일

| 파일 | 필요한 변경 |
| --- | --- |
| `server/desktop/admin/overlay.py` | 공유 인물 ID 라벨 생성 |
| `server/desktop/admin/camera_view.py` | `pid`·`gpid` 동시 표시와 `gpid:null`, 가독성·위치 조정 |
| `server/desktop/admin/video_worker.py` | 이벤트의 원래 로컬 ID 보존 |
| `server/desktop/admin/legacy_gui.py` | 공통 라벨·시각 포맷, 카드 하단 추가·오래된 카드 제거, 스크롤 참조 |
| `server/desktop/admin/application.py` | 이벤트 정렬·화면 갱신·스크롤, 상세 시각 변환 |
| `server/desktop/admin/display_time.py` | 화면용 한국시간 변환 함수 |
| `server/setup/install_helper/tests/` | 아래의 회귀 테스트 추가·보완 |

이번 변경은 관제 화면의 표시와 관련 테스트에 한정한다. 전처리·Data·Compose·모델·송출 설정 변경, 지연 최적화, DB 마이그레이션·데이터 초기화는 포함하지 않는다. 저장·통신의 UTC 기준은 유지한다.

## 6. 검증 계획과 성공 기준

| 구분 | 시나리오 | 통과 기준 |
| --- | --- | --- |
| 라벨 | 로컬 ID와 전역 ID 존재 | `pid:7 gpid:42`로 두 항목을 모두 읽을 수 있음 |
| 라벨 | 전역 ID 미판정 | 박스와 `pid:7`을 유지하고 `gpid:null` 표시 |
| 라벨 | 전역 ID 판정 완료 | 다음 정상 응답에서 `pid:7 gpid:null`이 `pid:7 gpid:42`로 갱신됨 |
| 좌표 | 단일·4분할, 가로·세로 여백, 가장자리 | 박스가 해당 인물 위치에 표시되고 라벨이 영역 밖으로 잘리지 않음 |
| 좌표 | 표시 OFF→ON | 선택이 저장되고 켰을 때 신선한 좌표가 표시됨 |
| 좌표 | 무관측·만료·미래 시각·조회 오류·유효한 빈 배열 | 기존 동작에 따라 오래된 박스를 숨기고, 유효한 빈 배열에서는 객체 라벨도 제거 |
| 좌표 | 송출 OFF→ON, 모델·전달 장애→복구 | 기존 복구 흐름을 유지하고 정상 좌표 수신 후 라벨 자동 갱신 |
| 세션 | 재접속 후 같은 로컬 ID 재사용 | 이전 세션의 전역 ID가 잘못 재사용되지 않음 |
| 이벤트 | 초기 최근 30건·새 이벤트 1건 | 오래된 이벤트가 위, 최신 이벤트가 아래, 최근 30건 유지 |
| 이벤트 | 중복 ID·동일 시각·늦게 저장된 이벤트 | 중복 없음, 동일 시각은 ID 순서, 늦은 이벤트도 발생 시각에 맞게 배치 |
| 이벤트 | 과거 내용 열람 중 새 이벤트 수신 | 읽는 위치 유지, 하단을 보던 경우에만 새 이벤트 따라감 |
| 이벤트 | 카메라 A→B→A | 해당 카메라의 목록과 순서가 정확히 복원됨 |
| 시각 | `2026-11-04T05:14:54.226Z` | `2026-11-04 14:14:54 KST` |
| 시각 | `2026-11-04T18:30:00Z` | `2026-11-05 03:30:00 KST` |
| 시각 | 같은 시점의 `Z`·`+00:00`·`+09:00` 입력 | 결과 동일, PC 시간대에 무관 |
| 시각 | 누락·비정상·시간대 없는 입력 | 충돌·현재 시각 위조 없이 확인 불가 표시 |
| 통합 | 실제 사람 등장→감지→identity 완료→퇴장 | 영상 박스·`pid`·`gpid:null`→판정된 `gpid` 전환·오름차순 타임라인·KST를 한 화면에서 확인 |

수정 시 먼저 위 표시 요구사항을 검증하는 테스트와 타임라인·시간 변환 결함을 재현하는 회귀 테스트를 추가하고 최소 수정 후 통과시킨다. 기존 좌표 기하 테스트, 데스크톱 이벤트 수집·상세·다중 카메라 테스트 중 변경 경로에 해당하는 검사를 실행한다. 이후 실제 Docker·카메라·PyQt 화면에서 위 통합 시나리오를 수행한다.

구현 전 원인 조사에서 수행한 확인:

- `python -m unittest server.setup.install_helper.tests.test_overlay -v`: **4건 통과**.
- 현행 `_render()`와 페인터 대역: 박스 표시와 ID 한 개 선택 동작 확인.
- 현행 `legacy_event()`: 로컬 ID 대체와 UTC 문자열 유지 확인.
- 현행 이벤트 저장·카드 생성 함수와 위젯 대역: 최신→과거 표시 및 UTC 원문 출력 재현.
- 현행 `get_live_objects()`와 설치 DB 메모리 사본: 조사 시 만료 응답, 관측 후 1초의 두 ID 포함 정상 응답 확인. 원본 DB 불변 확인.
- 이 조사 당시 실제 GUI·컨테이너 통합 검증: **미수행**.

## 7. 구현·검증 결과

- 영상 오버레이·이벤트 카드·이벤트 상세에 `pid`와 `gpid`를 함께 표시한다. 전역 ID가 아직 없을 때는 `gpid:null`을 쓴다. 이벤트 어댑터는 로컬 ID를 그대로 보존한다.
- 카드 추가 방향과 오래된 카드 제거 위치를 바꾸어 위에 과거 이벤트, 아래에 최신 이벤트를 표시한다. 같은 시각에는 이벤트 ID로 순서를 정한다. 카메라를 바꿀 때 목록을 다시 표시하며 사용자별 스크롤 위치와 하단 자동 따라가기를 반영한다.
- 이벤트 카드·상세·분석 갱신·관련 녹화 시각을 화면에서 KST로 변환한다. 시간대가 없거나 잘못된 값은 `시각 확인 불가`로 나타낸다.
- 변경 후 `python -m unittest server.setup.install_helper.tests.test_overlay server.setup.install_helper.tests.test_desktop_display_time -v`: **10건 통과**.
- 변경된 코드·테스트 10개 파일의 Python AST 구문 검사와 `git diff --check`: **통과**.
- PyQt 다중 카메라·이벤트 흐름 테스트는 추가했으나 **현재 환경에서 미실행**. `server/setup/install_helper/.venv/Scripts/python.exe`가 존재하지 않는 `C:\Users\gksrn\AppData\Local\Programs\Python\Python311\python.exe`를 가리키고, 사용 가능한 Python에는 PyQt5가 없다. 따라서 실기기·컨테이너 통합과 실제 GUI 렌더 확인은 남아 있다.

## 8. 완료·배포 기준

`pid`·`gpid` 동시 표시, 미판정 시 `gpid:null`, 타임라인 순서, KST 변환의 테스트와 실제 화면 검증이 통과하면 수정 완료로 판정한다. 기존 ID 미표시의 원인은 사용자가 확인한 시간 지연으로 기록하며 추가 원인 규명을 완료 조건으로 요구하지 않는다. 데스크톱 실행 파일을 다시 빌드·배포하고, 문제 발생 시 이전 데스크톱 빌드로 되돌릴 수 있도록 배포 버전을 남긴다.

계획의 화면 수정은 구현되었고 독립적으로 실행할 수 있는 회귀 테스트를 통과했다. 실제 PyQt 화면과 Docker·카메라 통합 확인 뒤 배포한다.
