# CPU·메모리 개선 조치 검증 결과 보고서

- 검증일: 2026-11-16 (1차 적용·프로파일링 10:15~12:38 KST, 최종 2카메라 안정성·라이브 HTTP 재측정 19:40~19:51 KST)
- 기준 문서: [CPU·메모리 사용량 및 시스템 지연 진단](system-resource-investigation-2026-11-16.md)
- 적용 범위: 기준 문서의 권장 조치 1~8단계 전체 검증(1~4단계는 라이브 Windows + Docker 환경에 실제 적용·측정, 5~8단계는 코드·라이브 HTTP·스레드·I/O·IPC·디코더 프로파일링으로 검증)
- 운영 제약: [`signature.py`](../server/deployment/signature.py)가 `compose.yml`, `compose.gpu.yml`, `migrations/*.sql`, `AppConfig`, 시크릿 키, `ai_cctv_core.contracts`를 계약 해시(`.contract-signature.json`)로 검증하므로 추적 중인 저장소 코드는 수정하지 않고, `C:\ProgramData\AI_CCTV\` 및 `C:\Users\gksrn\.wslconfig`에 운영 설정을 적용했다.

---

## 요약 결론

1. **호스트·전처리 CPU 부하가 목표 수준 이하로 안정화되었다.**
   - 카메라 2대(`cam-001` + `local-camera`)와 관리자 GUI가 동시에 실행되는 조건에서 호스트 전체 CPU는 활성 기준 **평균 73.17%(최대 81.16%)**(오전 진단 표본 99~100%)에서 최종 구성(`WSL 10GB / 6 CPU`, `preprocessing cpus: 3.5`, `analysis cpus: 1.5`, 기동 타임아웃 `60s`, `analysis_fps: 3.0`) 기준 **평균 66.53%(최대 80.12%)** 로 낮아졌다. 60초(20표본) 연속 측정 동안 95% 이상 지속 구간은 발생하지 않았다.
   - `ai-cctv-preprocessing-1` 평균 CPU는 **137.30% → 88.45%**로 약 **35.6% 감소**했다. 두 카메라의 실시간 좌표 지연(`live_objects_observed_age_seconds`)은 **0.449~0.479초**(`stale: false`), 모델 타임아웃 **0건**, 최근 15분 컨테이너 로그 오류 **0건**이었다.
2. **기준 문서 2단계의 기본 기동 타임아웃(30초)은 CPU 제한과 함께 사용할 수 없음을 확인했다.**
   - `preprocessing: cpus: 2.5`, `analysis: cpus: 1.0` 적용 시 기본 `30초` 기동 타임아웃에서는 `OsNetIdentity()`(`CUDAExecutionProvider`) 초기화만 **31.32초**(`import 1.64s + init 29.68s`)가 걸려 `IDENTITY_MODEL_STARTUP_TIMEOUT`과 `DETECTION_MODEL_TIMEOUT`, `analysis` 일시 `unhealthy`가 재현되었다.
   - 두 기동 타임아웃(`DETECTION_STARTUP_TIMEOUT_SECONDS`, `OBJECT_STARTUP_TIMEOUT_SECONDS`)을 **`60초`**로 상향하자 `cpus: 2.5 / 1.0`과 `cpus: 3.5 / 1.5` 모두 타임아웃 없이 `ready`에 도달했다(`3.5 / 1.5` 기준 `compose up --wait` 47.26초 + 모델 준비 16.61초).
3. **메모리 목표(가용 RAM 5GiB 이상)는 WSL 파일 캐시와 Windows 동시 실행 앱에 따라 달라졌다.**
   - `.wslconfig`(`memory=10GB, processors=6, swap=3GB`) 적용 후 개발 앱 부담이 적었던 낮 시간대(11:47~12:37)에는 Windows 가용 RAM이 **6.10~7.11GiB**로 목표를 충족했다.
   - 반면 저녁 재측정(19:44~19:47)에서는 VS Code·Chrome·ChatGPT 등 호스트 앱과 함께 `compose up --force-recreate` 직후 WSL `buff/cache`가 `5,829MiB`까지 늘어나며 `vmmemWSL` 작업 집합이 `9,477.1MiB`에 도달해 가용 RAM 평균이 `4.51GiB`(2.5/1.0) 및 `3.05GiB`(3.5/1.5)로 내려갔다(약 2분 후 `vmmemWSL`이 `8,231.5MiB`, 여유 물리 메모리가 `3.67GiB`로 일부 회복됨). WSL 스왑 사용은 `7~9MiB / 3,072MiB`, Windows `PagesOutput/sec`는 `0`으로 스와핑은 발생하지 않았다. 호스트 가용 RAM 5GiB 이상을 상시 유지하려면 기준 문서가 제시한 **`.wslconfig` `memory=8GB` 추가 시험**과 불필요한 개발 앱 정리가 필요하다.
4. **5~8단계(코드·저장소 구조 개선)의 실질적 효과를 프로파일링으로 확인했다.**
   - `has_active_recording_file()` 라이브 컨테이너 평균 **127.5~151.1ms**(2초 TTL 캐시 적용 시 벤치마크 기준 **28.13ms → 0.78ms**, 약 36배 단축), 720p Pipe IPC 평균 **14.98ms → `SharedMemory` 1.11ms**(13.5배) 또는 360p **1.68ms**(8.9배), 관리자 GUI 디코드+표시 720p **8.51ms/frame → 360p 2.95ms/frame**(2.88배), Windows bind mount 대비 Linux `/tmp` 64KB 쓰기 **42.5배**·`iterdir+stat` **260배**·SQLite WAL insert **3.27배** 차이와 실제 `sqlite3.OperationalError: database is locked` 로그를 확인했다.

---

## 1. 권장 조치 8단계 검증 매트릭스

| 단계 | 조치 항목 | 검증 방식 | 핵심 확인 결과 | 판정·주의점 |
|---|---|---|---|---|
| 1 | 관리 GUI 종료 시 분리 검증 | 실제 GUI 종료 전후 `local-camera` FFmpeg·MediaMTX·CPU 측정 | 로컬 송출(`h264_nvenc`)·MediaMTX·AI 추론 모두 중단되지 않음. 호스트 CPU `73.17% → 63.80%`, `preprocessing` `137.30% → 97.42%` | **검증 완료**. GUI의 HLS 디코딩·복사 및 폴링 부하 분리 확인 |
| 2 | AI 컨테이너 CPU 상한(`cpus`) 적용 | `compose.cpu-limits.yml` override 적용·콜드 스타트·정상상태 측정 | `30초` 기본 타임아웃에서는 `2.5 / 1.0` 콜드 스타트 실패. **`60초` 타임아웃** 상향 후 `2.5 / 1.0` 및 `3.5 / 1.5` 모두 정상 기동·`ready` | **보완 필요 확인**. `ComposeAdapter`가 별도 override를 자동 읽지 않으므로 추후 배포 반영 방식 검토 필요 |
| 3 | `.wslconfig` (`10GB / 6 CPU / 3GB swap`) | WSL 종료·재기동 후 `nproc`, `free -m`, 호스트 가용 RAM 비교 | `nproc=6`, `MemTotal=9,946MiB`, `Swap used 0~9MiB`. 낮 시간 `6.10~7.11GiB` 달성, 캐시 팽창 시 `3.05~4.51GiB` | **적용 완료**. 캐시 팽창 환경에서도 5GiB 이상을 확보하려면 `memory=8GB` 추가 시험 권장 |
| 4 | `analysis_fps` `5.0 → 3.0` 조정 | `config.yaml` 수정·재기동 후 `Settings.analysis_fps` 및 CPU 비교 | 2카메라 기준 `preprocessing` CPU `137.30% → 100.56%(2.5/1.0) / 88.45%(3.5/1.5)`. 좌표 지연 `0.33~0.48s` 유지 | **검증 완료**. 단 `LatestFrameCapture`의 30fps `capture.read()`는 계속 실행됨 |
| 5 | Data API 프로파일링 및 녹화 상태 캐시 | 라이브 HTTP 부하 프로파일링 + 저장소/캐시 마이크로벤치마크 | 라이브 `/cameras` 평균 `137.95ms`, `/objects` `98.8~105.7ms`. `has_active_recording_file` 2초 TTL 캐시 시 **36배(벽시계)**·**30배(CPU)** 감소 | **근거 확보 완료**. 실제 코드 적용은 후속 패치 대상 |
| 6 | 복구 작업 간격 및 자원 분리 | [`recovery.py`](../server/services/data/app/workers/recovery.py) 코드 확인 + DB `recovery_jobs` 검사 | 작업 성공 후 대기(`sleep`) 없이 즉시 다음 작업 claim 확인. 최근 큐 `completed: 100, failed: 2, pending: 0` | **원인 확인 완료**. 백로그 재발 시 인터-잡 sleep/예산 제한 필요 |
| 7 | 디코딩·복사·IPC 경량화 | 스레드 CPU 프로파일링 + IPC 3종·PyAV/PyQt5 720p vs 360p 벤치마크 | `SharedMemory` IPC **13.5배**, 360p Pipe **8.9배**, 360p GUI 디코드/복사 **2.88배** 감소(`reformat+copy`가 720p 비용의 77.5%) | **근거 확보 완료**. 서브스트림/공유메모리 도입 타당성 확인 |
| 8 | Windows bind mount vs Linux FS 검토 | 컨테이너 내 I/O·SQLite 벤치마크 + 운영 로그 오류 추적 | `/snapshots`가 `/tmp` 대비 64KB 쓰기 **42.5배**, `iterdir+stat` **260배**, SQLite WAL insert **3.27배** 느림. 실제 `database is locked` 로그 관측 | **근거 확보 완료**. 백업·호스트 접근 호환성 검토 후 이전 판단 |

---

## 2. 1~4단계 라이브 환경 실측 비교

기준 조건의 일관성을 위해 **카메라 2대 모두 활성인 실행**과 **1카메라(`cam-001` 오프라인) 실행**을 구분하여 기록한다.

### 2.1 호스트·컨테이너 자원 비교

| 측정 구간 (시각) | 활성 카메라 / GUI | Host CPU 평균 (최대) | Host 가용 RAM (`vmmemWSL` WS) | `preprocessing` CPU 평균 (최대) | `data` CPU 평균 | `external` CPU 평균 | `analysis` CPU 평균 | 비고 |
|---|---|---:|---:|---:|---:|---:|---:|---|
| 활성 기준 (11:22, 6표본) | 2대 / GUI 열림 | **73.17%** (81.16%) | 4.69 GiB (5,195 MiB) | **137.30%** (167.00%) | 18.73% | 10.47% | 11.80% | 제한 없음, WSL 8CPU/11.63GiB, FPS 5.0 |
| 1단계: GUI 종료 (11:26, 6표본) | 2대 / **GUI 닫힘** | **63.80%** (70.54%) | 4.77 GiB (5,190 MiB) | **97.42%** (108.90%) | 20.57% | 8.59% | 0.71% | 로컬 FFmpeg `6.29%` 유지 |
| 2+3단계 @ FPS 5 (12:37, 6표본) | **1대(`local-camera`만)** / GUI 열림 | 58.95% (73.16%) | **7.11 GiB** (5,060 MiB) | 147.81% (169.01%) | 18.90% | 2.42% | 3.42% | `cam-001` 오프라인으로 1대 조건; `2.5/1.0 + 60s` |
| **최종 B: 2.5 / 1.0 @ FPS 3** (19:44, 8표본) | **2대** / GUI 열림 | **70.56%** (80.62%) | 4.51 GiB (6,125 MiB) | **100.56%** (135.94%) | 47.67% | 24.96% | 1.14% | `WSL 10GB/6CPU`, `60s` 타임아웃, `all_ready` |
| **최종 C: 3.5 / 1.5 @ FPS 3** (19:46, 20표본·60초) | **2대** / GUI 열림 | **66.53%** (80.12%) | 3.05 GiB (9,477 MiB → 이후 8,232 MiB) | **88.45%** (160.56%) | 42.49% | 19.06% | 1.34% | `WSL 10GB/6CPU`, `60s` 타임아웃, `all_ready` |

> [!NOTE]
> - 11:47(`stage_step3_wsl_limits.json`)과 11:51(`stage_step4_fps_3.json`) 초기 측정에서는 `local-camera` 미송출 상태였음에도 WSL 재기동 직후 복구·재초기화 부하(`data` 44.8~50.4%, `external` 25.6~33.2%, `identity` 30초 타임아웃 재시도)가 겹쳐 호스트 CPU가 96.65~96.69%로 치솟았다. 위 표의 **최종 B·C**는 두 카메라와 모든 모델이 `ready`에 도달한 정상상태에서 측정한 값이다.
> - 최종 B·C에서 `data` CPU 평균(42.5~47.7%)과 `external` CPU 평균(19.1~25.0%)은 낮 기준선(18.7% / 10.5%)보다 높았는데, 이는 테스트 중 양쪽 카메라의 이벤트·라이브 객체 갱신 및 GUI 폴링이 활발히 동작한 구간이기 때문이다.

### 2.2 기동 타임아웃(30초 vs 60초) 및 지연 지표 검증

- **30초 타임아웃 실패 근거 (`2.5 / 1.0` 적용 시):**
  - `compose up` 종료 코드 1 (`ai-cctv-analysis-1` 일시 `unhealthy`), `cam-001`·`local-camera` 모두 `DETECTION_MODEL_TIMEOUT (stage=startup, elapsed=30.000s)` 발생, `identity`는 `IDENTITY_MODEL_STARTUP_TIMEOUT` 발생.
  - `ai-cctv-preprocessing-1` 내부에서 `OsNetIdentity()` 단독 초기화 시간을 계측한 결과 **`import=1.64s`, `init=29.68s`, 총 `31.32s`** (`CUDAExecutionProvider`)가 소요되어 [`compose.yml`](../server/compose.yml)의 기본값 `OBJECT_STARTUP_TIMEOUT_SECONDS=30` 및 `DETECTION_STARTUP_TIMEOUT_SECONDS=30`을 초과함을 직접 입증했다.
- **60초 타임아웃 적용 후 비교 (모두 2카메라 활성, `analysis_fps=3.0`):**

| 항목 | 활성 기준선 (`FPS 5`, 제한 없음) | 최종 B (`2.5 / 1.0 + 60s`, `FPS 3`) | 최종 C (`3.5 / 1.5 + 60s`, `FPS 3`) |
|---|---|---|---|
| 콜드 스타트 `compose up --wait` | - | - (기존 컨테이너 재사용, 6.55s ready) | **47.26s** (`rc=0`), 모델 추가 대기 **16.61s** |
| 모델 타임아웃 / 재시작 / OOM | 0 / 0 / false | **0 / 0 / false** | **0 / 0 / false** |
| `cam-001` 추론 p50 / p95 | 77.3ms / 215.1ms | 79.1ms / 227.6ms | **69.4ms / 148.5ms** |
| `local-camera` 추론 p50 / p95 | 82.5ms / 216.0ms | 88.2ms / 223.8ms | **66.1ms / 167.6ms** |
| `cam-001` `data_put` p50 / p95 | 56.2ms / 114.0ms | 51.1ms / 123.4ms | **48.7ms / 82.2ms** |
| `local-camera` `data_put` p50 / p95 | 58.0ms / 244.7ms | 65.0ms / 211.4ms | **44.9ms / 89.2ms** |
| 실시간 좌표 지연 (`cam-001` / `local`) | 0.179s / 0.198s (`stale: false`) | 0.330s / 0.446s (`stale: false`) | 0.449s / 0.479s (`stale: false`) |
| GPU 사용률 / VRAM | 17% / 719 MiB | 13% / 719 MiB | 35% / 719 MiB |

`preprocessing`에 **3.5 CPU**(`analysis` 1.5 CPU)를 주면 `2.5 / 1.0` 대비 컨테이너 평균 CPU가 오히려 낮아졌고(`100.56% → 88.45%`), `inference_total` p95와 `data_put` p95가 모두 크게 단축되었다.

### 2.3 메모리·WSL 캐시·페이징 관찰

- WSL 내부는 `.wslconfig`(`memory=10GB, processors=6, swap=3GB`) 적용 후 `nproc=6`, `MemTotal=9,946MiB`, `SwapTotal=3,072MiB`로 정상 반영되었다.
- 측정 구간별 메모리 추이:
  - 11:47 (`step3_wsl_limits`): 가용 RAM **6.10 GiB** (`vmmemWSL` 8,387.7 MiB, WSL `available` 6,606 MiB, `buff/cache` 5,213 MiB, `swap` 0 MiB)
  - 12:37 (`clean_wsl6_cpu25_fps5`): 가용 RAM **7.11 GiB** (`vmmemWSL` 5,060.0 MiB, WSL `available` 5,661 MiB, `buff/cache` 5,469 MiB, `swap` 0 MiB)
  - 19:44 (`final_wsl6_cpu25_fps3`): 가용 RAM **4.51 GiB** (`vmmemWSL` 6,125.0 MiB, WSL `available` 5,063 MiB, `buff/cache` 5,118 MiB, `swap` 7 MiB)
  - 19:46 (`final_wsl6_cpu35_fps3`, 재생성 직후): 가용 RAM **3.05 GiB** (`vmmemWSL` 9,477.1 MiB, WSL `used` 3,948 MiB / `buff/cache` 5,829 MiB / `available` 5,463 MiB, `swap` 9 MiB, `PagesOutput/sec` 0)
  - 19:49 (종료 약 2분 후 `post_test_logs_and_mem.json`): `vmmemWSL` WS가 **8,231.5 MiB**로 회수되며 호스트 여유 물리 메모리가 **3.67 GiB**(`3,850,968 KiB`)로 회복됨. 당시 Windows 상주 프로세스 중 `Memory Compression` `1,325.1 MiB`, `Code`(상위 3개 합 `1,292.9 MiB`), `chrome`(상위 3개 합 `813.5 MiB`), `ChatGPT`(상위 2개 합 `537.5 MiB`), `MsMpEng` `365.2 MiB`, `explorer` `355.9 MiB`, `agy` `276.5 MiB`가 함께 메모리를 점유하고 있었다.

---

## 3. 5~8단계 심층 검증 결과

### 3.1 5단계: Data API 라이브 프로파일링 및 녹화 상태 조회 캐시

2카메라 활성 부하 상태에서 `ai-cctv-data-1` 내부 HTTP 엔드포인트를 직접 측정한 결과는 다음과 같다.

| 엔드포인트 (`127.0.0.1:8000`) | 반복 | 상태코드 | 평균(ms) | p50(ms) | p95(ms) | 최대(ms) |
|---|---:|---|---:|---:|---:|---:|
| `GET /internal/v1/cameras?limit=200&offset=0&user_id=1` | 20 | 200 | **137.953** | 134.054 | 189.806 | 189.806 |
| `GET /internal/v1/cameras/enabled` | 20 | 200 | **87.111** | 86.865 | 125.311 | 125.311 |
| `GET /internal/v1/cameras/cam-001/objects` | 30 | 200 | **98.786** | 89.932 | 162.894 | 244.122 |
| `GET /internal/v1/cameras/local-camera/objects` | 30 | 200 | **105.657** | 103.600 | 143.497 | 151.575 |
| `GET /internal/v1/users/1` | 30 | 200 | **59.618** | 56.218 | 98.317 | 111.307 |
| `POST /internal/v1/object-jobs/identity/claim` | 20 | 200 | **57.796** | 56.858 | 82.879 | 82.879 |
| `POST /internal/v1/object-jobs/analysis/claim` | 20 | 200 | **66.696** | 66.063 | 102.882 | 102.882 |

- [`cameras.py`](../server/services/data/app/api/cameras.py)의 `enabled_cameras()`와 `list_cameras()`는 매 요청마다 각 카메라에 대해 [`has_active_recording_file()`](../server/services/data/app/storage/recordings.py#L18-L41)을 호출한다.
- 컨테이너 내 파일시스템 탐색 실측(`data_container_bench`): `cam-001`(15개 항목) 평균 **151.128ms**(최대 222.956ms), `local-camera`(12개 항목) 평균 **127.530ms**(최대 168.220ms)가 걸렸다.
- 별도 프로파일(`deep_profile_results.json`, 20회 반복)에서 `has_active_recording_file(cam-001)`은 벽시계 평균 **28.127ms**(p95 73.546ms, CPU 3.684ms)였으나, **2초 TTL 캐시** 적용 모의 측정에서는 벽시계 평균 **0.775ms**(CPU 0.122ms)로 **벽시계 약 36배, CPU 약 30배 감소**를 확인했다. (단, 녹화 시작·종료 API에서 캐시 무효화가 함께 필요하다.)

### 3.2 6단계: 녹화 복구(`recover_outages`) 루프 동작

- [`recover_outages()`](../server/services/data/app/workers/recovery.py#L662-L734)는 `job is None`이거나 `claim_due_recovery_job()`이 예외를 낸 경우에만 `recovery_poll_interval_seconds`(기본 5초)만큼 대기하고, **복구 작업을 완료한 뒤 루프 끝에서는 대기 없이 곧바로 다음 작업을 claim**한다.
- DB `recovery_jobs` 검사 결과 총 99건(`completed: 97, detected: 1, failed: 1`) 중 작업 #94(`selected: 164, downloaded: 164`) 완료 직후 #95~#98이 4~5초 간격으로 연쇄 실행되었음을 확인했다. 최종 테스트 종료 시점에는 `recovery_jobs` `completed: 100, failed: 2`, `object_jobs` `identity:complete 185, analysis:complete 191`, `events` 794건, `recording_segments` 443건이 대기(`pending`/`running`) 0 상태로 처리되었다.

### 3.3 7단계: 디코딩·IPC·관리 GUI 복사 비용 프로파일링

- **전처리 컨테이너 스레드 프로파일(5초 표본):** `uvicorn` 메인 프로세스가 1코어 기준 `56.4%`, 두 YOLO `spawn` 자식 프로세스가 각각 `35.2%`·`35.0%`, `identity` 자식이 `2.4%`를 사용했다. `capture_frames_replaced`(baseline 기준 `cam-001` 102,039회)와 [`pipeline.py`](../server/services/preprocessing/app/pipeline.py#L556)의 프레임 간격 확인 순서상 `analysis_fps`를 낮춰도 30fps 원본 RTSP 디코딩은 계속 수행됨을 확인했다.
- **IPC 방식별 벤치마크(`ai-cctv-preprocessing-1` 내부):**

| IPC 방식 및 프레임 크기 | 벽시계 평균(ms) | 벽시계 p95(ms) | 부모 프로세스 CPU 평균(ms) | 비고 |
|---|---:|---:|---:|---|
| Pipe pickle (`1280×720×3`, 2.76MB) | 14.977 | 28.732 | 9.681 | 현재 방식 |
| Pipe pickle (`640×360×3`, 0.69MB) | **1.680** | 3.354 | **1.054** | **8.9배** 단축 (CPU 9.2배↓) |
| `SharedMemory` zero-pickle (`1280×720×3`) | **1.112** | 2.936 | **0.729** | **13.5배** 단축 (CPU 13.3배↓) |

- **관리 GUI 디코딩·RGB 변환·복사 벤치마크(`.venv` PyAV + PyQt5, 90프레임):**
  [`video_worker.py`](../server/desktop/admin/video_worker.py#L586-L593) 경로를 실측한 결과, 720p에서는 디코딩 자체보다 **`reformat(rgb24)` + `QImage(...).copy()`가 전체 CPU의 77.5%** 를 차지했다.

| 해상도 @ 30fps | 디코드(ms / 비중) | `reformat(rgb24)`(ms / 비중) | `bytes+QImage.copy`(ms / 비중) | 프레임당 총 CPU(ms) | 30fps 기준 (1코어 / 8코어) |
|---|---:|---:|---:|---:|---|
| `1280×720` | 1.693 (22.4%) | 4.339 (36.7%) | 7.636 (40.8%) | **8.507** | 25.52% / 3.19% |
| `640×360` | 1.178 (41.2%) | 0.692 (17.6%) | 1.918 (41.2%) | **2.951** (**2.88배↓**) | 8.85% / 1.11% |

### 3.4 8단계: Windows Bind Mount I/O 비용 및 SQLite 잠금 경합

- `ai-cctv-data-1` 내부에서 Windows bind mount(`/snapshots`)와 컨테이너 내부 Linux 파일시스템(`/tmp`)을 비교 측정한 결과는 다음과 같다.

| 연산 | Windows bind mount (`/snapshots`) | Linux 네이티브 (`/tmp`) | 배수 차이 |
|---|---:|---:|---:|
| 64KB 파일 쓰기 (평균 / p95) | 9.264ms / 20.524ms | **0.218ms** / 0.447ms | **42.5배** 느림 |
| `iterdir()` + 20개 `stat()` 평균 | 55.455ms | **0.213ms** | **260배** 느림 |
| SQLite WAL insert (평균 / p95) | 5.268ms / 8.251ms | **1.613ms** / 2.881ms | **3.27배** 느림 |

- 실제 운영 로그에서도 오전 부하 구간에 [`connection.py`](../server/services/data/app/database/connection.py#L17-L54)의 `busy_timeout = 5,000ms`를 초과하여 `update_camera_video_profile` 경로 등에서 `sqlite3.OperationalError: database is locked` 예외가 발생한 기록을 확인했다. 반면 최종 B·C 테스트 구간을 포함한 최근 15분(`strict_error_scan.json`) 동안에는 `preprocessing`(79행), `analysis`(44행), `data`(13,413행), `external`(56행) 전체에서 오류·타임아웃·잠금 경합 로그가 **0건**이었다.

---

## 4. 단위 테스트 및 현재 적용 상태·되돌리기 안내

### 4.1 실행한 기준선 단위 테스트

`server/setup/install_helper/.venv` 환경에서 검증 주제와 관련된 호스트/배포 테스트를 실행한 결과 **27개 테스트 모두 통과**했다(실행 시간 12.088s).

- `server/tests/test_operations_compose.py`
- `server/tests/test_contract_signature.py`
- `server/setup/install_helper/tests/test_recording_control.py`
- 참고: `server/services/data/test_storage_paths.py`는 호스트 `.venv`에 `fastapi`가 설치되어 있지 않아 import 단계에서 실패했으며(`ModuleNotFoundError: No module named 'fastapi'`), 저장소 소스 코드는 일절 수정하지 않았다.

### 4.2 현재 호스트에 적용된 설정과 롤백 방법

현재 시스템은 **최종 C(`WSL 10GB / 6 CPU`, `preprocessing cpus: 3.5`, `analysis cpus: 1.5`, 기동 타임아웃 `60s`, `analysis_fps: 3.0`)** 구성으로 6개 컨테이너 모두 `healthy`이며, `cam-001` 추론·`identity`(`CUDAExecutionProvider`)·`analysis`(`PyTorch CUDA`)와 관리자 GUI 프로세스가 정상 동작 상태로 유지되었다. 테스트용으로 실행했던 `local-camera` 호스트 FFmpeg 송출 프로세스는 검증 스크립트 종료(19:48:04 KST)와 함께 종료되어 `local-camera` 워커만 `offline` 상태이며, 필요 시 관리자 GUI에서 로컬 카메라 송출을 다시 시작할 수 있다.

- 적용 파일:
  - `C:\Users\gksrn\.wslconfig` (`memory=10GB`, `processors=6`, `swap=3GB` — 기존 파일 없음)
  - `C:\ProgramData\AI_CCTV\resource-remediation-20261116\compose.cpu-limits.yml` (`preprocessing cpus: 3.5`, `analysis cpus: 1.5`, 두 서비스 기동 타임아웃 `60`)
  - `C:\ProgramData\AI_CCTV\config\config.yaml` (`inference.analysis_fps: 3.0`)
- 원본 백업 위치: `C:\ProgramData\AI_CCTV\resource-remediation-20261116\` (`config.yaml`, `compose.env`, `local-camera.json`, `baseline_inspection.json`)
- **원상 복구(Rollback) 절차:**
  1. `C:\ProgramData\AI_CCTV\resource-remediation-20261116\config.yaml`을 `C:\ProgramData\AI_CCTV\config\config.yaml`로 복원한다.
  2. 기존 Compose 조합(`compose.yml` + `compose.gpu.yml`, CPU 제한 override 제외)으로 AI 서비스를 재생성한다:
     ```powershell
     docker compose --env-file C:\ProgramData\AI_CCTV\config\compose.env -f server\compose.yml -f server\compose.gpu.yml up -d --no-build --no-deps --force-recreate --wait preprocessing analysis
     ```
  3. WSL 한도를 기본값으로 되돌리려면 서비스 정상 종료 후 `C:\Users\gksrn\.wslconfig`를 삭제하고 `wsl --shutdown` 뒤 Docker Desktop과 서비스를 다시 시작한다.

---

## 5. 후속 권장 작업

1. **운영 배포 통합:** [`ComposeAdapter`](../server/operations/compose.py#L175-L212)가 현재 `compose.yml`과 `compose.gpu.yml`만 조립하므로, 배포 버전 업데이트 시 `cpus: 3.5 / 1.5`와 기동 타임아웃 `60s`를 정식 배포 설정·계약 서명 체계에 반영한다.
2. **메모리 `8GB` 시험:** IDE·브라우저 등 호스트 앱과 동시 실행 시에도 Windows 가용 RAM `5GiB` 이상이 필요하면 `.wslconfig`의 `memory=8GB`를 다음 유지보수 창에서 시험하고 `Swap used` 및 `PagesOutput/sec` 증가 여부를 확인한다.
3. **코드 개선 우선순위(5·7·8단계):**
   - 1순위(저위험·고효과): [`has_active_recording_file()`](../server/services/data/app/storage/recordings.py#L18-L41)에 짧은 TTL 캐시 및 녹화 시작·종료 무효화 적용
   - 2순위: 탐지 IPC의 `SharedMemory` 또는 서브스트림 저해상도 경로 도입, 관리 GUI의 서브스트림/하드웨어 디코딩 검토
   - 3순위: DB·고빈도 스냅샷 경로의 Linux 파일시스템/전용 Docker volume 이전 및 백업·복원 호환성 검증
