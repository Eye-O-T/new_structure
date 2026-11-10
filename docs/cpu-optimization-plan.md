# CPU 사용량 절감 작업 계획서

작성일: 2026-11-10  
대상 범위:
1. **우선순위 1 (가장 안전하고 효과가 큰 항목)**
   - **작업 1-1**: 데스크톱 관제 GUI(`CameraView._render`)의 배경 `QPixmap` 캐싱 (`Qt.SmoothTransformation` 화질 유지)
   - **작업 1-2**: `ai-cctv-data-1`의 `live_objects` 인메모리 서빙 + `person_track_presence` DB 갱신 주기 1.0초 스로틀링 (`ACTIVE_OBSERVATION_SECONDS = 3`초 충돌 판정 기준 보존)
2. **우선순위 2 (설계·인수 조건 확인 후 결정할 항목)**
   - **작업 2-1**: 로컬 카메라 FFmpeg(`server.local_camera_publisher`)의 `h264_nvenc` 우선 적용 및 실패 시 `libx264` 자동 폴백

---

## 1. 배경 및 실측 기준선 (Baseline)

로컬 카메라(`local-camera`, `1280×720@30fps`) 송출 및 데스크톱 관제 GUI 실행 상태에서 측정한 시스템 자원 사용량은 다음과 같다.

| 구분 | 프로세스 / 컨테이너 | 주요 작업 / 스레드 | CPU 사용량 (8 논리 코어 기준) | 메모리 / 지연 지표 |
|---|---|---|---|---|
| 호스트 | `ffmpeg.exe` (`server.local_camera_publisher`) | DirectShow `1280×720@30fps` 캡처 + `hflip` + `libx264` (`veryfast`, `zerolatency`) | **시스템 31.9%** (코어 255.2%) | WS 86.4 MB |
| 호스트 | `python.exe` (데스크톱 관제 GUI) | `TID 30704` (Qt 메인 스레드 `_render`: 코어 56.8%)<br>`TID 33480` (`VideoWorker._decode_loop`: 코어 49.5%) | **시스템 20.6%** (코어 165.1%) | WS 190.5 MB |
| 컨테이너 | `ai-cctv-preprocessing-1` | RTSP 디코딩 + `YoloTracker`(`cuda:0`) + `OsNetIdentity`(`cuda:0`) | 코어 77.4% ~ 88.9% | MEM 1.765 GiB / VRAM 368 MiB |
| 컨테이너 | `ai-cctv-data-1` | 초당 5회 `PUT /objects` 및 초당 5회 `GET /objects` SQLite 트랜잭션 | **코어 34.6% ~ 64.7%** | `db_lock_wait` p50 **59.2ms** (p95 **154.3ms**)<br>`data_put` p50 **230.6ms** (p95 **599.5ms**) |

---

## 2. 우선순위 1: 가장 안전하고 효과가 큰 항목

### 작업 1-1. 데스크톱 GUI (`CameraView._render`) 배경 `QPixmap` 캐싱

#### 1) 원인 및 목표
- 현재 [`CameraView._render`](../server/desktop/admin/camera_view.py)는 새 비디오 프레임 도착(`set_frame`, 초당 30회)뿐 아니라 실시간 객체 오버레이 갱신(`set_objects`, 초당 5회) 및 오버레이 토글(`set_overlay_enabled`) 시마다 호출된다.
- 호출될 때마다 원본 `1280×720` `QImage`(`self._frame`)를 `QPixmap.fromImage(self._frame)`로 변환하고 `source.scaled(display_width, display_height, Qt.KeepAspectRatio, Qt.SmoothTransformation)`를 다시 계산한다.
- **목표**: `Qt.SmoothTransformation`의 고화질 스케일링은 그대로 유지하면서, 프레임(`self._frame`)과 위젯 표면 크기(`surface.width()`, `surface.height()`)가 동일할 때는 스케일링된 배경 `QPixmap`을 재사용하여 `set_objects` 호출(초당 5회) 시 발생하는 불필요한 이미지 재스케일링 CPU 부하를 제거한다.

#### 2) 수정 대상 파일
- [`server/desktop/admin/camera_view.py`](../server/desktop/admin/camera_view.py)
- [`server/setup/install_helper/tests/test_multi_camera_view.py`](../server/setup/install_helper/tests/test_multi_camera_view.py) (단위 테스트 추가)

#### 3) 상세 구현 명세
1. **캐시 상태 필드 추가** (`CameraView.__init__`):
   - `self._cached_base_pixmap: QPixmap | None = None`: 레터박스/필러박스 여백을 포함한 `(width, height)` 크기의 스케일링 완료 배경 `QPixmap`.
   - `self._cached_base_key: tuple[int, int, int, int, int] | None = None`: `(frame_cache_key, width, height, display_width, display_height)` 튜플. (`QImage.cacheKey()`를 사용하여 동일 객체 주소 재사용 시에도 프레임 변경을 정확히 감지).
2. **배경 생성 및 재사용** (`CameraView._render`):
   - `width = max(self.surface.width(), 320)`, `height = max(self.surface.height(), 200)` 및 `display_width`, `display_height` 계산 후 `base_key = (self._frame.cacheKey(), width, height, display_width, display_height)`를 생성한다.
   - `self._cached_base_pixmap is None`이거나 `self._cached_base_key != base_key`인 경우에만:
     - `source = QPixmap.fromImage(self._frame)`
     - `scaled = source.scaled(display_width, display_height, Qt.KeepAspectRatio, Qt.SmoothTransformation)`
     - `(width, height)` 크기의 검정(`QColor(7, 13, 23)`) 배경 `QPixmap` 위에 `scaled`를 중앙 정렬로 그린 뒤 `self._cached_base_pixmap` 및 `self._cached_base_key`에 저장한다.
   - 오버레이를 그리지 않는 경우(`not self._overlay_enabled` 또는 객체 만료/빈 목록):
     - 추가 `QPixmap` 복사나 `QPainter` 생성 없이 `self.surface.setPixmap(self._cached_base_pixmap)`를 바로 호출한다.
   - 오버레이를 그리는 경우:
     - `canvas = self._cached_base_pixmap.copy()` 위에 바운딩 박스와 라벨만 `QPainter`로 합성한 뒤 `self.surface.setPixmap(canvas)`를 호출한다.
3. **캐시 무효화 조건**:
   - `show_stopped()`, `show_error()`, `show_loading()`(`self._frame is None`인 경우) 호출 시 `self._cached_base_pixmap = None`, `self._cached_base_key = None`으로 초기화한다.

#### 4) 부작용 및 안전성 검토
- `Qt.SmoothTransformation`을 그대로 사용하므로 영상 화질이나 텍스트/경계선 품질 저하가 전혀 없다.
- 프레임당 캐시 메모리는 단일 `QPixmap`(약 `1280×720×4B ≈ 3.6MB` 이하) 1개뿐이며, 새 프레임 도착 시 즉시 교체되므로 메모리 누수가 발생하지 않는다.

---

### 작업 1-2. `ai-cctv-data-1`의 `live_objects` 인메모리 서빙 + `person_track_presence` 1.0초 주기 스로틀링

#### 1) 원인 및 목표
- `preprocessing`의 `LivePublisher`는 최신 바운딩 박스 좌표를 초당 최대 5회(`0.2초` 간격) `PUT /cameras/{camera_id}/objects`로 전송하고, `external`/GUI는 초당 5회 `GET /cameras/{camera_id}/objects`로 조회한다.
- 현재 [`ObjectRepositoryMixin.put_live_objects`](../server/services/data/app/database/repositories/objects.py)는 매 요청마다 Windows 호스트 바인드 마운트(`C:\ProgramData\AI_CCTV\database`, WSL2 9P 파일시스템) 위 SQLite 연결을 새로 열고 `BEGIN IMMEDIATE` 트랜잭션으로:
  1. `live_objects` 테이블에 JSON 문자열을 UPSERT하고,
  2. 프레임 내 모든 객체에 대해 [`note_track_observation`](../server/services/data/app/database/repositories/identity.py)을 호출하여 `person_track_presence` 테이블을 매번 UPSERT한다.
- `live_objects`는 3초(`LIVE_OBJECTS_MAX_AGE_SECONDS = 3`)가 지나면 즉시 폐기되는 휘발성 실시간 오버레이 데이터이며, `person_track_presence`는 동시 등장 트랙 간 글로벌 ID 충돌 방지([`conflicting_global_ids`](../server/services/data/app/database/repositories/identity.py)의 `ACTIVE_OBSERVATION_SECONDS = 3`초 기준)에만 사용된다. 또한 출현/퇴장 이벤트 발생 시점에는 [`create_event`](../server/services/data/app/database/repositories/events.py)가 별도로 `note_track_observation`을 즉시 기록하고 있다.
- **목표**:
  1. `live_objects` 최신 상태는 `DataRepository`의 스레드 안전 인메모리 딕셔너리에서 즉시 갱신·서빙하여 `live_objects` 테이블에 대한 초당 5회 디스크 쓰기를 제거한다.
  2. `put_live_objects` 내부의 `person_track_presence` DB 갱신은 **트랙(`(camera_id, tracking_session_id, person_id)`) 최초 관측 시 즉시 기록**하고, 이후 동일 활성 트랙에 대해서는 **마지막 DB 반영 후 `1.0초` 이상 경과했을 때만** 단일 트랜잭션으로 갱신한다(`ACTIVE_OBSERVATION_SECONDS = 3`초 판정 창 대비 3배 안전 여유 확보).
  3. 검출 객체가 없는 빈 프레임(`objects=[]`)은 인메모리 상태만 갱신하고 SQLite 쓰기 트랜잭션을 아예 열지 않는다.

#### 2) 수정 대상 파일
- [`server/services/data/app/database/repositories/__init__.py`](../server/services/data/app/database/repositories/__init__.py)
- [`server/services/data/app/database/repositories/objects.py`](../server/services/data/app/database/repositories/objects.py)
- [`server/services/data/app/database/repositories/identity.py`](../server/services/data/app/database/repositories/identity.py)
- [`server/services/data/app/database/repositories/cameras.py`](../server/services/data/app/database/repositories/cameras.py)
- [`server/services/data/app/database/repositories/retention.py`](../server/services/data/app/database/repositories/retention.py)
- [`server/services/data/app/database/repositories/test_live_objects.py`](../server/services/data/app/database/repositories/test_live_objects.py)

#### 3) 상세 구현 명세
1. **인메모리 상태 구조 및 동기화** (`DataRepository.__init__`):
   - `self._live_objects_lock = threading.RLock()`
   - `self._live_objects_by_camera: dict[str, dict[str, Any]] = {}`
     - 저장 항목: `camera_id`, `tracking_session_id`, `observed_at`(문자열), `observed_at_dt`(`datetime`), `frame_width`, `frame_height`, `objects`(깊은 복사된 dict 리스트), `updated_at`.
   - `self._track_presence_flushed_at: dict[tuple[str, str, str], datetime] = {}`
     - 키: `(camera_id, tracking_session_id, person_id)`, 값: 마지막으로 `person_track_presence`에 기록된 `observed_at_dt`(`datetime`).
   - 상수 정의: `TRACK_PRESENCE_FLUSH_INTERVAL_SECONDS = 1.0` (`ACTIVE_OBSERVATION_SECONDS = 3`초의 1/3).
2. **`put_live_objects(camera_id, payload)` 동작 흐름**:
   - `observed_at_dt = parse_utc(payload.observed_at)` 파싱.
   - `with self._live_objects_lock:`:
     - 기존 인메모리 레코드가 존재하고 `observed_at_dt <= current["observed_at_dt"]`이면 역전된 지연 패킷이므로 **메모리 상태와 DB 모두 갱신하지 않고** 기존 스냅샷 기준 응답을 반환한다 (`test_late_older_payload_does_not_replace_newer_observation` 계약 100% 보존).
     - 최신 패킷이면 `self._live_objects_by_camera[camera_id]`를 새 스냅샷으로 갱신한다.
     - 해당 카메라의 이전 `tracking_session_id`와 다른 새 세션이면 `_track_presence_flushed_at`에서 해당 카메라의 구 세션 키를 정리한다.
     - `payload.objects` 중 DB에 `note_track_observation`을 기록해야 할 대상(`objects_to_flush`)을 선별한다:
       - 트랙 키 `(camera_id, payload.tracking_session_id, item.person_id)`가 `_track_presence_flushed_at`에 없거나,
       - `(observed_at_dt - last_flushed_dt).total_seconds() >= TRACK_PRESENCE_FLUSH_INTERVAL_SECONDS` (`1.0초`)인 경우에만 포함.
   - `objects_to_flush`가 비어 있지 않은 경우에만 `with self._transaction() as connection:`을 열어 선별된 트랙들에 대해 `self.note_track_observation(...)`을 수행하고 `_track_presence_flushed_at`을 갱신한다.
   - `objects_to_flush`가 비어 있으면(빈 프레임이거나 모든 트랙이 1.0초 이내에 이미 기록된 경우) **SQLite 연결/트랜잭션을 전혀 수행하지 않는다.**
   - 마지막으로 `self.get_live_objects(camera_id)` 결과를 반환한다.
3. **`get_live_objects(camera_id)` 동작 흐름**:
   - 카메라 활성 여부 확인: `cameras` 테이블에서 `SELECT enabled FROM cameras WHERE camera_id = ?` 조회하여 카메라가 없거나 `enabled = 0`이면 기존과 동일하게 `stale=True, stale_reason="no_observation", objects=[]` 반환.
   - `with self._live_objects_lock:`에서 `self._live_objects_by_camera.get(camera_id)`를 조회한다. (인메모리에 없으면 `stale=True, stale_reason="no_observation"` 반환).
   - `observed_age = (now - record["observed_at_dt"]).total_seconds()` 검증:
     - `observed_age < 0` → `stale=True, stale_reason="future_timestamp", objects=[]`
     - `observed_age > LIVE_OBJECTS_MAX_AGE_SECONDS` (`3초`) → `stale=True, stale_reason="expired", objects=[]`
   - 유효한 객체가 존재할 때만 `person_identity_links` 테이블에서 `(camera_id, tracking_session_id)`의 `(person_id, global_person_id)` 매핑을 읽기 전용으로 조회하여 각 객체에 `global_person_id`를 주입한다 (객체가 빈 리스트 `[]`이면 DB 조회조차 생략).
4. **카메라 비활성화 / 삭제 / 보관주기 정리 연동**:
   - [`update_camera`](../server/services/data/app/database/repositories/cameras.py)에서 `enabled=False`로 변경되거나 [`delete_camera`](../server/services/data/app/database/repositories/cameras.py) 호출 시 `_live_objects_by_camera` 및 `_track_presence_flushed_at`에서 해당 `camera_id` 항목을 즉시 제거한다.
   - [`purge_expired_history`](../server/services/data/app/database/repositories/retention.py) 실행 시 만료된 인메모리 `_live_objects_by_camera` 및 `_track_presence_flushed_at` 항목도 함께 정리한다.

#### 4) 부작용 및 안전성 검토
- `data` 서비스는 단일 프로세스(`uvicorn --workers 1`)로 구동되며, 재시작 시에도 `preprocessing`이 0.2초 이내에 새 `live_objects`를 푸시하므로 오버레이 공백이 없다.
- `person_track_presence`는 새 트랙 출현 즉시 DB에 기록되고 이후 매 1.0초마다 갱신되므로, `ACTIVE_OBSERVATION_SECONDS = 3`초를 기준으로 하는 `conflicting_global_ids` 동시 트랙 충돌 판정 결과는 기존(0.2초마다 갱신)과 수학적으로 동일하다.

---

## 3. 우선순위 2: 설계·인수 조건 확인 후 결정할 항목

### 작업 2-1. 로컬 카메라 FFmpeg의 `h264_nvenc` 적용 (실패 시 `libx264` 자동 폴백)

#### 1) 원인 및 목표
- 호스트에서 실행되는 `server.local_camera_publisher`의 `ffmpeg.exe`가 `1280×720@30fps` 영상을 `libx264`로 소프트웨어 인코딩하며 전체 시스템 CPU의 **31.9%**(코어 기준 **255.2%**)를 점유하고 있다.
- 현재 호스트 GPU(`GTX 1650 with Max-Q Design`)는 3D 연산 19~24%, VRAM `458 MiB / 4096 MiB`만 사용 중이며 하드웨어 인코더(NVENC)는 유휴(0%) 상태이다.
- **목표**: 로컬 카메라 퍼블리셔 시작 시 NVIDIA 하드웨어 인코더(`h264_nvenc`)를 우선 시도하고, GPU 미지원·드라이버 오류·세션 제한 등으로 실패할 경우 기존 `libx264` 소프트웨어 인코더로 자동 폴백하도록 구성한다.

#### 2) 사전 결정 및 확인 사항 (인수 조건)
1. **인코더 옵션 호환성**:
   - 현재 [`_build_command`](../server/local_camera_publisher/__main__.py)에 하드코딩된 `-tune zerolatency`, `-keyint_min`, `-sc_threshold 0`은 `libx264` 전용이거나 `h264_nvenc`에서 지원하지 않는 플래그이다.
   - `h264_nvenc` 사용 시에는 MediaMTX RTSP/HLS 세그먼트 규격(`2초` GOP, B-프레임 비활성화, `yuv420p`)을 만족하는 NVENC 전용 저지연 인자(`-c:v h264_nvenc -preset p1 -tune ull -rc cbr -b:v 2500k -maxrate 2500k -bufsize 5000k -g <gop> -bf 0 -pix_fmt yuv420p`)로 분기해야 한다.
2. **인코더 선택 정책 제어**:
   - 기본값은 `auto`(`h264_nvenc` 우선 시도 후 `libx264` 폴백)로 두되, 환경변수 `AI_CCTV_LOCAL_CAMERA_ENCODER`(`auto` | `h264_nvenc` | `libx264`)를 통해 언제든 `libx264`로 강제 고정할 수 있도록 한다.

#### 3) 수정 대상 파일
- [`server/local_camera_publisher/__main__.py`](../server/local_camera_publisher/__main__.py)
- [`server/setup/install_helper/tests/test_live_video.py`](../server/setup/install_helper/tests/test_live_video.py) (커맨드 빌더 및 폴백 순서 단위 테스트 추가)

#### 4) 상세 구현 명세
1. **인코더별 FFmpeg 인자 분기 (`_build_command`)**:
   - `_build_command(args, *, explicit_dshow_mode: bool, encoder: str = "libx264") -> list[str]` 시그니처로 확장한다.
   - `encoder == "h264_nvenc"`일 때:
     ```python
     [
         "-c:v", "h264_nvenc",
         "-preset", "p1",
         "-tune", "ull",
         "-rc", "cbr",
         "-b:v", "2500k",
         "-maxrate", "2500k",
         "-bufsize", "5000k",
         "-g", gop,
         "-bf", "0",
         "-pix_fmt", "yuv420p",
     ]
     ```
   - `encoder == "libx264"`일 때:
     - 기존 인자(`-c:v libx264 -preset veryfast -tune zerolatency -g <gop> -keyint_min <gop> -sc_threshold 0 -bf 0 -pix_fmt yuv420p`)를 그대로 유지한다.
2. **단계별 기동 및 자동 폴백 (`main`)**:
   - `_candidate_encoders()`가 환경변수 `AI_CCTV_LOCAL_CAMERA_ENCODER`에 따라 기본 `("h264_nvenc", "libx264")` 순서로 후보를 반환한다.
   - `(encoder, explicit_dshow_mode)` 조합을 순서대로 시도한다:
     1. `("h264_nvenc", True)`
     2. `("h264_nvenc", False)`
     3. `("libx264", True)`
     4. `("libx264", False)`
   - NVENC 초기화는 CUDA 컨텍스트 생성에 약 `0.3~0.6초`가 소요될 수 있으므로, 초기 조기 종료 판정 대기 시간을 `h264_nvenc`의 경우 `1.0초`(0.2초 × 5회 폴링), `libx264`의 경우 기존 `0.4초`로 둔다.
   - 또한 스트리밍 도중 `h264_nvenc` 프로세스가 비정상 종료(`child.wait() != 0`)하고 실행 유지 시간이 `5.0초` 미만이었다면, 다음 루프에서 즉시 `libx264`로 강등(demote)하여 무한 재시작 루프를 방지한다.
   - `write_runtime_state(data_root, ...)` 기록 시 현재 활성화된 `encoder`(`h264_nvenc` 또는 `libx264`) 필드를 함께 저장하여 `local-camera.json`에서 실제 적용된 인코더를 즉시 확인할 수 있게 한다.

---

## 4. 단계별 검증 계획 및 완료 기준

### 4.1 단위 및 계약 테스트 (공통)
1. **우선순위 1-1 (GUI 캐싱)**:
   - `python -m unittest server/setup/install_helper/tests/test_multi_camera_view.py server/setup/install_helper/tests/test_overlay.py`
   - 검증 항목:
     - 동일 프레임(`set_frame`)에서 `set_objects`가 여러 번 호출될 때 `QPixmap.fromImage` / `scaled` 재호출 없이 `_cached_base_pixmap`이 재사용되는지 확인.
     - 새 프레임(`set_frame`) 입력, 위젯 크기 변경(`resizeEvent`), `show_stopped`/`show_error` 호출 시 캐시가 정상 갱신/무효화되는지 확인.
2. **우선순위 1-2 (`data` 인메모리 `live_objects` + 1.0초 스로틀링)**:
   - `python -m unittest server/services/data/app/database/repositories/test_live_objects.py server/services/data/app/database/repositories/test_edge_identity.py`
   - 검증 항목:
     - 기존 `LiveObjectsRepositoryTests` 3개 케이스(`stale_reason`, 역전 타임스탬프 무시, `global_person_id` 조인) 통과.
     - 신규 테스트: 0.2초 간격으로 5회 `put_live_objects` 호출 시 첫 호출(0.0초)과 1.0초 경과 시점에만 `person_track_presence`가 갱신되고, 그 사이(0.2s, 0.4s, 0.6s, 0.8s)에도 `get_live_objects`는 최신 좌표를 반환하며 `conflicting_global_ids`가 해당 글로벌 ID를 정상 차단하는지 확인.
     - 카메라 비활성화(`enabled=False`) 또는 삭제 시 인메모리 객체가 즉시 비워지는지 확인.
3. **우선순위 2-1 (로컬 카메라 `h264_nvenc` + 폴백)**:
   - `python -m unittest server/setup/install_helper/tests/test_live_video.py`
   - 검증 항목:
     - `_build_command`가 `h264_nvenc`와 `libx264` 각각에 대해 올바른 인자 목록을 생성하는지 확인.
     - `h264_nvenc` 실패 시 `libx264`로 자동 폴백하는지 확인.

### 4.2 런타임 실측 검증 기준 (로컬 카메라 ON 상태)
| 측정 대상 | 변경 전 기준선 | 목표 기준 (완료 판정) | 확인 방법 |
|---|---|---|---|
| 데스크톱 GUI 메인 스레드 (`TID 30704`) | 코어 56.8% | 코어 **35% 이하** (프레임 미변경 `set_objects` 시 스케일링 0회) | 호스트 프로세스/스레드 CPU 프로파일링 |
| `ai-cctv-data-1` 컨테이너 CPU | 코어 34.6% ~ 64.7% | 코어 **15% 이하** | `docker stats --no-stream` |
| `preprocessing` → `data` DB 락 대기 (`db_lock_wait_seconds` p95) | p95 154.3ms | p95 **30ms 이하** | `GET /diagnostics/preprocessing` (`live_Status`) |
| 호스트 `ffmpeg.exe` CPU (우선순위 2 적용 시) | 시스템 31.9% (코어 255.2%) | 시스템 **8% 이하** (코어 **65% 이하**), `local-camera.json` 내 `"encoder": "h264_nvenc"` | 호스트 프로세스 프로파일링 및 `C:\ProgramData\AI_CCTV\runtime\local-camera.json` |

---

## 5. 롤백(복원) 절차

1. **작업 1-1 (`CameraView._render`)**:
   - [`server/desktop/admin/camera_view.py`](../server/desktop/admin/camera_view.py) 변경분 되돌리기 후 데스크톱 관제 GUI 재시작.
2. **작업 1-2 (`ai-cctv-data-1`)**:
   - [`server/services/data/app/database/repositories/objects.py`](../server/services/data/app/database/repositories/objects.py) 등 변경분 되돌리기 후 `docker compose up -d --build data` 재기동 (SQLite 스키마 변경이 없으므로 데이터베이스 마이그레이션/복원 불필요).
3. **작업 2-1 (`local_camera_publisher`)**:
   - 코드 수정 없이 즉시 `libx264`로 복원하려면 환경변수 `AI_CCTV_LOCAL_CAMERA_ENCODER=libx264` 설정 후 로컬 카메라 토글 재시작, 또는 [`server/local_camera_publisher/__main__.py`](../server/local_camera_publisher/__main__.py) 되돌리기 후 로컬 카메라 재시작.
