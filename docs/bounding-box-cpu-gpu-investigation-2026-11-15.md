# 바운딩 박스 누락 및 CPU/GPU 사용량 조사

조사 시각: 2026-11-15 18:37~18:40 KST. 실행 중인 Docker 컨테이너, 내부 상태/좌표 API, 읽기 전용 DB 조회, 호스트 GPU 및 소스코드를 대조했다. 운영 설정·컨테이너·코드는 변경하지 않았다. 수치는 해당 시점의 표본이며 지속 평균이 아니다.

## 결론

**현재 AI가 GPU 비활성화 상태의 CPU 이미지로 실행되고 있으며, 추론 지연과 모델 시간 초과 때문에 최신 좌표가 3초 유효기간을 넘는다. Data API가 만료된 좌표를 빈 목록으로 반환하므로 실시간 영상에는 박스가 표시되지 않는다.**

이벤트는 별도 저장 경로이므로 늦게 처리된 등장·퇴장·분석 결과가 타임라인에 남을 수 있다. 실제 양쪽 카메라의 등장 이벤트와 분석 완료 이벤트를 확인했다. `analysis` 컨테이너는 외관 속성 분석을 담당하며 실시간 박스 생성의 선행 조건이 아니다.

## 1. 직접 확인한 상태

| 항목 | 파이 `cam-001` | 로컬 `local-camera` |
|---|---|---|
| DB 상태 | online, 송출/녹화 활성 | online, 송출/녹화 활성 |
| 전처리 캡처 | connected=true, frame_stale=false | connected=true, frame_stale=false |
| 실제 탐지 실행 장치 | cpu | cpu |
| 모델 준비 상태, 18:37:56 | true | false |
| 최근 추론 시간 | 5.238초 | 마지막 성공 5.522초 |
| 최근 표본 추론 p50 / p95 | 1.656 / 6.857초 | 4.736 / 9.220초 |
| 모델 시간 초과 누계 | 5 | 6 |
| 최근 좌표 상태 | stale=true | stale=true |

로컬 카메라의 오류는 `DETECTION_MODEL_TIMEOUT`, `stage=startup`, `elapsed=30.007s`였다. 최근 로그에는 양쪽 카메라의 `stage=process` 약 10초 시간 초과 및 초기화 약 30초 시간 초과가 반복된다. 실패 후 모델 재시도 간격은 30초다. 이 과정에서 박스 공급이 더 오래 중단된다.

전처리 컨테이너는 첫 조회에서 `unhealthy`였고 다음 조회에서는 `healthy`로 바뀌었다. 최근 healthcheck에는 HTTP 503과 200이 섞여 있다. 컨테이너 실행 여부나 단일 healthcheck 성공만으로 탐지 정상 여부를 판단할 수 없다.

## 2. 박스가 사라지는 정확한 지점

현재 흐름은 다음과 같다.

`RTSP 캡처 → CPU YOLO/추적 → 관측 시각을 유지한 좌표 PUT → Data의 3초 만료 검사 → Desktop 오버레이`

18:39:36~18:39:41 KST에 External 서비스의 정상 내부 조회 권한으로 Data 좌표 API를 세 번 조회했다.

| 카메라 | HTTP | stale_reason | 관측 후 경과(초), 세 표본 | 수신 후 경과(초), 세 표본 |
|---|---|---|---|---|
| cam-001 | 200 | expired | 7.644 / 6.450 / 8.536 | 4.287 / 0.065 / 2.151 |
| local-camera | 200 | expired | 287.640 / 289.889 / 291.550 | 281.938 / 284.187 / 285.848 |

모든 응답의 `objects`는 빈 목록이었다. 특히 파이의 두 번째 응답은 **수신 후 0.065초밖에 지나지 않았는데 관측 후 6.450초**였다. 결과 전달 직후에도 이미 만료됐다는 직접 증거다. 단순 화면 좌표 변환 문제만으로 이 응답을 설명할 수 없다.

소스 근거:

- [유효기간 상수](../lib/ai_cctv_core/contracts/objects.py): `LIVE_OBJECTS_MAX_AGE_SECONDS = 3`.
- [탐지 처리](../server/services/preprocessing/app/pipeline.py): `_process_frame()`이 추론을 마친 뒤 캡처 당시 `observed_at`을 유지하여 좌표를 발행한다.
- [Data 좌표 조회](../server/services/data/app/database/repositories/objects.py): `get_live_objects()`가 관측 시각 기준 3초 초과이면 `stale=true`, `stale_reason=expired`, `objects=[]`를 반환한다.
- [Desktop 표시](../server/desktop/admin/camera_view.py): `set_objects()`가 stale 결과를 제거한다. 정상 결과도 수신 후 3초를 넘기면 그리지 않는다.

파이 표본의 모델 실행 5.216초에 비해 IPC는 0.021초였다. 최근 좌표 PUT의 p95는 파이 0.243초, 로컬 1.570초였다. 로그에는 로컬 PUT의 2초 시간 초과도 있다. **모델 실행이 주된 지연이며, API/DB 대기는 추가 악화 요인**이다. 현 표본만으로 DB 부하의 세부 원인까지 확정하지는 않았다.

## 3. CPU는 높고 GPU는 낮은 이유

### 확정: AI 컨테이너가 CPU 모드다

- 운영 파일 `C:\ProgramData\AI_CCTV\config\compose.env`: `PREPROCESSING_GPU=false`, `INFERENCE_DEVICE=auto`, `ANALYSIS_DEVICE=auto`.
- 실행 이미지: `ai-cctv-preprocessing:0.3.0`, `ai-cctv-analysis:0.3.0`.
- 두 컨테이너의 Docker `DeviceRequests`는 `null`: GPU 할당이 없다.
- 전처리 PyTorch: `2.8.0+cpu`, `torch.version.cuda=null`, `torch.cuda.is_available()=false`.
- 실제 상태: YOLO=`cpu`, OSNet=`cpu / CPUExecutionProvider`, 속성 분석=`cpu / PyTorch CPU`.

`auto`가 GPU를 켜주는 설정은 아니다. [Settings.from_env()](../server/services/preprocessing/app/settings.py)는 `PREPROCESSING_GPU=false`이면 `auto`를 `cpu`로 해석한다. [Compose 실행 구성](../server/operations/compose.py)은 GPU 플래그가 참일 때만 [compose.gpu.yml](../server/compose.gpu.yml)을 추가한다. 현재는 CPU 빌드이므로 실행 장치 문자열만 바꿔도 GPU 추론은 사용할 수 없다.

### 실측 부하

`docker stats --no-stream` 표본:

| 컨테이너 | CPU | 메모리 |
|---|---:|---:|
| preprocessing | 467.12% | 818.3 MiB / 4 GiB |
| data | 70.88% | 87.4 MiB |
| mediamtx | 14.20% | 15.4 MiB |
| external | 4.48% | 83.4 MiB |
| nginx | 2.35% | 4.3 MiB |
| analysis | 0.60% | 427.8 MiB |

Docker CPU 100%는 논리 CPU 한 개 기준이다. 전처리 467%는 약 4.7개 논리 CPU 사용에 해당하며, 호스트 전체 CPU 사용률 467%라는 뜻은 아니다. 호스트는 i7-8565U, 4코어/8스레드다.

전처리 내부 2초 표본에서는 모델 자식 프로세스가 225.9%, 서비스 프로세스가 65.3%였다. CPU 추론이 주요 소비처임을 뒷받침한다. 새 진단 프로세스 기준 PyTorch 기본 계산 스레드는 4, interop은 4, OpenCV는 8이었다. 실행 중인 각 모델의 내부 스레드 설정 전체를 측정한 값은 아니므로 스레드 경합의 정확한 기여율은 미확정이다.

추가 CPU 경로도 존재한다. [전처리 캡처](../server/services/preprocessing/app/camera_capture.py)는 OpenCV/FFmpeg로 스트림을 계속 읽고 최신 프레임을 교체하며, [Desktop](../server/desktop/admin/video_worker.py)은 PyAV 디코딩·RGB 변환·이미지 복사를 수행한다. 두 경로에 명시적인 GPU 디코딩 설정은 없다. 추론 목표는 카메라당 5 FPS지만 실측 추론은 수 초이므로 지속적으로 목표를 놓치는 상태다. `analysis_fps`를 낮춰도 입력 스트림 디코딩은 계속된다.

### GPU 사용량 일부는 송출 인코딩으로 설명된다

- NVIDIA GPU: GeForce GTX 1650 with Max-Q Design, 드라이버 566.36.
- `nvidia-smi` 표본: GPU 11%, encoder 7%, decoder 0%, 메모리 79 MiB.
- 호스트 로컬 FFmpeg의 실제 인코더: `h264_nvenc`.

따라서 GPU 사용량이 조금 보인다는 사실은 AI가 GPU를 사용한다는 증거가 아니다. 현재 GPU 할당·PyTorch 빌드·모델 상태는 모두 AI의 CPU 실행을 가리킨다. GPU 이미지 `preprocessing:0.3.0-gpu`, `analysis:0.3.0-gpu`는 로컬에 있지만 이번 조사에서 실행하거나 호환성을 검증하지 않았다.

## 4. 타임라인은 실제 기록되고 있다

읽기 전용 DB 조회로 확인한 예:

| 카메라 | 이벤트 | ID | 발생 시각(KST) |
|---|---|---:|---|
| cam-001 | person_appeared | 359 | 18:28:26.307 |
| cam-001 | analysis_completed | 360 | 18:28:28.990 |
| local-camera | person_appeared | 366, 367 | 18:34:40.797 |
| local-camera | analysis_completed | 368 | 18:35:13.075 |
| local-camera | analysis_completed | 369 | 18:35:28.136 |

18:38:34 KST 기준 작업 집계는 속성 분석 complete=49, 인물 연결 complete=38 / failed=7 / running=4였다. 이는 보관 DB 전체 집계이며 모두 이번 장애에서 발생했다고 해석하면 안 된다.

이벤트와 분석 결과는 저장되어 나중에 조회할 수 있다. 실시간 좌표에 적용되는 3초 만료 규칙과 다르므로, 타임라인 기록과 박스 누락은 동시에 발생할 수 있다.

## 5. 조치 우선순위 및 검증 기준

이번 요청은 원인 조사·보고이므로 아래 조치는 적용하지 않았다.

1. **GPU 배포 경로 검증 및 전환**: 기존 GPU 이미지·호스트 드라이버·Docker GPU 접근의 호환성을 확인하고, GPU 플래그와 Compose GPU 구성을 함께 적용하여 AI 컨테이너를 재생성한다. 검증은 `DeviceRequests` 존재, CUDA 지원 PyTorch, YOLO/OSNet/분석 상태의 실제 GPU 장치 선택으로 한다. 플래그 수정만 하고 기존 컨테이너를 단순 재시작하는 것으로 끝내면 안 된다.
2. **두 카메라 동시 부하 검증**: 사람이 보이는 조건에서 좌표 API가 `stale=false`와 실제 bbox를 반환하고 Desktop에 표시되는지 확인한다. 관측부터 화면 도달까지 p95가 3초 이내여야 하며 여유를 확보해야 한다. 모델 초기화/추론 시간 초과가 재발하는지도 관찰한다.
3. **남는 CPU 부하 분리 측정**: GPU 전환 후 모델·입력 디코딩·Desktop 출력·Data 부하를 다시 측정한다. 필요할 때 입력 해상도/FPS, CPU 스레드 수, 하드웨어 디코딩을 개별 비교한다. 현재 표본만으로 특정 스레드 수나 해상도를 최적값으로 단정하지 않는다.
4. **만료시간/타임아웃 확대는 근본 해결로 삼지 않는다**: 느린 좌표를 더 오래 표시하거나 재시도를 늦출 수는 있지만 실시간성은 개선하지 않는다. CPU 임시 운영이 필요하면 단일 카메라 등으로 부하를 낮춘 뒤 실제 좌표 지연을 다시 측정한다.

## 조사 범위의 한계

- 프런트엔드의 박스 표시 체크박스 설정 자체는 확인하지 못했다. 다만 서버가 이미 빈 목록을 반환하므로 해당 설정과 무관하게 현재 서버 측 누락 원인은 성립한다.
- GPU 전환 후의 성능·VRAM 여유·드라이버 호환성은 실행 검증 전이다.
- 소스 수정·운영 설정 변경·컨테이너 재시작·추가 모델 추론 벤치마크는 수행하지 않았다.
