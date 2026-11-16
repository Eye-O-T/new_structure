# CPU 부하·메모리 사용량 감소 작업 계획서

- 작성일: 2026-11-16 KST
- 대상: Windows 호스트, WSL 2/Docker, AI CCTV 서버 및 관리자 GUI, `cam-001`·`local-camera`
- 상태: 진행 중. W3 녹화 상태 캐시를 코드에 반영하고 단위 검증했다. W0~W2, W4~W8은 아래 실행 기록과 환경 접근 조건에 따라 진행한다.
- 근거: [최초 조사](system-resource-investigation-2026-11-16.md), [Gemini 개선 조치 검증 보고서](system-resource-remediation-verification-2026-11-16.md)
- 전제: 검증 보고서의 최종 C 구성을 출발 후보로 삼되, 실행 시 실제 구성과 두 카메라의 송출 상태를 다시 확인한다.

## 1. 목적과 판단

**현재 CPU 개선 설정을 재배포 후에도 유지하고, Windows 가용 메모리 5GiB 이상을 확보하면서 API·영상 복사·파일 탐색의 반복 비용을 줄인다.** CPU 상한과 메모리 한도를 일괄 축소하는 방식 대신, 항목별 변경 전후를 같은 조건에서 비교한다.

Gemini는 최초 권장 조치 1~4의 운영 설정을 적용했고, 5~8은 프로파일링·벤치마크로 개선 근거를 확보했다. 따라서 후속 작업은 이미 적용한 설정의 배포 통합과 미적용 코드 개선이다. GPU 추론 재전환이나 메모리 누수를 전제로 한 구조 변경은 범위에 포함하지 않는다.

| 구분 | 검증 보고서의 확인 결과 | 계획에 반영할 판단 |
|---|---|---|
| CPU | 2카메라·GUI 조건에서 호스트 평균 73.17% → 66.53%, 전처리 137.30% → 88.45% | 최종 C를 후보 기준으로 유지하고 장시간 재검증 |
| CPU 상한 | `2.5 / 1.0`보다 `3.5 / 1.5` 구간에서 지연이 짧았음 | 더 낮은 상한을 우선 목표로 삼지 않음. 표본·부하가 달라 인과관계 확정은 보류 |
| 기동 | 30초 설정에서 초기화 실패, 60초에서 준비 완료 | CPU 제한과 기동 타임아웃 60초를 함께 배포 |
| 메모리 | WSL 10GB에서도 저녁 가용 RAM 3.05~4.51GiB, 캐시 일부 회수 후 3.67GiB | 8GB 비교 시험과 호스트 앱 점유 분리 필요 |
| Data API | 녹화 파일 탐색 반복, 2초 TTL 모의 캐시의 함수 비용 감소 | 첫 번째 코드 패치로 범위가 작은 캐시 도입 |
| 영상·IPC | 360p 및 공유메모리 경로에서 개별 비용 감소 | GUI 복사 절감 → 서브스트림 → 남은 IPC 병목 순으로 적용 |
| 복구 | 완료 후 즉시 다음 작업 claim, 최종 대기 0·실패 2건 | 백로그를 재현해 제한 효과 확인. 기존 실패를 신규 실패와 구분 |
| 저장소 | Windows bind mount와 `/tmp`의 큰 I/O 차이, 과거 SQLite 잠금 오류 | 실제 영구 volume에서 재측정 후 DB·스냅샷을 개별 이전 |

### 근거의 한계와 보완

- 최종 C의 CPU 측정은 60초·20표본이다. 최초 99~100% 표본, 낮의 1카메라 결과, 저녁 2카메라 결과를 같은 부하의 A/B 시험으로 취급하지 않는다.
- TTL 36배, IPC 13.5배, GUI 2.88배, 파일 탐색 260배는 개별 연산의 벤치마크 결과다. 시스템 CPU나 RAM이 같은 배수로 줄어든다는 뜻은 아니다.
- GUI 표의 720p 단계별 시간 합계(`1.693 + 4.339 + 7.636ms`)는 기재된 총 CPU 시간 `8.507ms`와 일치하지 않는다. 벽시계·CPU 시간 및 비중의 분모를 원자료에서 확인하고, 재측정 전까지 단계별 절감량 산정에는 사용하지 않는다.
- 보고서에 제시된 JSON 원자료는 실행 준비 단계에서 위치·측정 조건을 확인한다. 본 계획 작성 시 운영 원자료와 현재 실행 이미지를 독립 재검증하지 않았다.
- WSL 작업 집합에 컨테이너 메모리를 더하지 않는다. Windows 앱 작업 집합 합계도 공유 페이지를 중복 포함할 수 있다. 파일 캐시·실사용·swap을 분리하고 장시간 증가 추세로 누수 여부를 판단한다.

## 2. 유지할 구성과 공통 완료 기준

### 출발 구성

| 항목 | 검증 보고서 최종 C | 후속 방침 |
|---|---|---|
| WSL | `memory=10GB`, `processors=6`, `swap=3GB` | CPU·swap은 유지하고 메모리만 8GB 비교 시험 |
| 전처리 / 분석 CPU 상한 | `3.5 / 1.5` | 현 장비의 검증 후보값으로 배포 통합 |
| 기동 타임아웃 | `DETECTION_STARTUP_TIMEOUT_SECONDS=60`, `OBJECT_STARTUP_TIMEOUT_SECONDS=60` | 실제 소비하는 서비스에 동일하게 전달되는지 확인 |
| 분석 FPS | 운영 `config.yaml`의 `inference.analysis_fps: 3.0` | 유지. `.env`에 값만 추가하는 방식은 사용하지 않음 |
| 전처리 메모리 상한 | 4GiB | 우선 유지. 다른 서비스 상한도 피크 측정 없이 추가하지 않음 |
| 추론 장치 | YOLO·속성 분석 CUDA, OSNet `CUDAExecutionProvider` | 실제 추론 및 장치 선택 유지 |

아래 수치는 **후속 작업의 제안 인수 기준**이다. 기존 60초 결과가 이미 충족했다고 간주하지 않는다. 기능 기준을 먼저 만족하고 CPU·메모리 효과를 평가한다.

| 항목 | 완료 기준과 측정 방식 |
|---|---|
| 호스트 CPU | 정상상태 30분 평균 ≤80%, 95% 이상이 30초 연속 발생하지 않음. 추가 최적화 목표는 새 기준선 대비 평균 10% 상대 감소 |
| Windows 메모리 | 합의한 평상시 호스트 앱 구성에서 정상상태 30분의 10초 표본 모두 가용 RAM ≥5GiB. 미달 시 메모리 목표는 미완료로 기록 |
| 메모리 안정성 | 새 OOM·비정상 재시작 0건. WSL swap·Windows 페이지 입출력의 지속 증가가 없고, 2시간 관찰에서 워밍업 이후 실사용 메모리의 지속 우상향이 없음 |
| 좌표·추론 | 카메라별 좌표 나이 p95 ≤1초, 3초 초과 만료·stale 비율이 기준선보다 증가하지 않음. 모델·실시간 PUT timeout 신규 0건 |
| API | `/cameras`, `/objects`, claim의 p95가 새 기준선 대비 10% 넘게 악화하지 않음. 동일 부하·요청 경로로 비교 |
| 작업 처리 | 정상 영상의 처리 FPS·인물 연결·속성 분석 완료 처리량이 기준선 대비 10% 넘게 감소하지 않음. 사람 등장·이동·이탈·재등장 확인 |
| 녹화·복구 | 계획된 유지보수 중단을 제외한 신규 녹화 공백·손상·누락 0건. 유효한 복구 백로그는 완료가 늘고 가장 오래된 대기시간이 지속 증가하지 않음 |
| DB·배포 | 신규 `database is locked` 0건, 백업 복원 성공, GUI/CLI 재기동·업데이트 후 같은 설정 유지, 기존 데이터·자격 증명 보존 |

호스트 앱 구성이 달라진 결과는 별도 시나리오로 기록한다. 앱 종료만으로 확보한 RAM과 서버 자체의 메모리 감소를 구분한다. 한도를 10GB에서 8GB로 줄여도 Windows RAM이 정확히 2GB 증가한다고 가정하지 않는다.

## 3. 작업 순서와 산출물

공수는 착수 후의 예상 순수 작업일이며 운영 일정 확정값이 아니다. 각 작업을 별도 변경 단위로 관리하고 검증에 통과한 구성만 다음 기준선으로 삼는다.

| ID | 우선순위 / 예상 공수 | 작업·주담당 역할 | 선행 조건 | 완료 산출물 |
|---|---|---|---|---|
| W0 | P0 / 0.5~1일 | 기준 재측정·복귀 자료 확보 / 운영·검증 | 없음 | 2카메라 기준선, 백업·이미지·설정 목록 |
| W1 | P0 / 1~2일 | 자원 설정의 정식 배포 통합 / 배포 | W0 | 배포 패치, 계약 호환 업그레이드 시험 |
| W2 | P0 / 0.5~1일 + 관찰 | WSL 8GB 비교 시험 / 운영 | W0 | 10GB↔8GB 비교 및 채택·복원 결정 |
| W3 | P1 / 1~2일 | 녹화 상태 TTL 캐시 / Data | W0 | 캐시 패치·무효화 시험·API 비교 |
| W4 | P1 / 1~2일 | GUI 변환·복사 및 숨김 화면 조회 절감 / Desktop | W3 후 새 기준선 | GUI 패치·동기화·요청량 비교 |
| W5 | P2 / 2~4일 | 분석·미리보기 서브스트림 / 영상·Edge·Desktop | W4, 원본·서브스트림 제공 가능 확인 | 별도 입력 경로, 좌표 매핑·정확도 검증 |
| W6 | P2 / 2~4일, 조건부 | 탐지 IPC 공유메모리 / 전처리 | W5 후 IPC 비용이 유의하게 남음 | 수명 관리·장애 복구 패치·IPC 비교 |
| W7 | P2 / 1~2일 | 복구 작업 사이 휴식 / Data | W3, 재현 가능한 복구 백로그 | 제한 패치·백로그 처리 시험 |
| W8 | P3 / 2~4일 + 이전 창 | DB·스냅샷 영구 volume 이전 / 저장소·배포 | W1·W3, 실 volume 이점·복원 확인 | 경로·백업 호환 패치, 이전·복귀 리허설 |

권장 실행 흐름은 **W0 → W1 → W2 → W3 → W4 → W5 → W6 필요성 판단 → W7 → W8 필요성 판단**이다. W7은 백로그 부하가 재발하면 W4보다 앞당긴다. W2가 실패해도 10GB로 복원하고 코드 개선을 계속하되 메모리 목표 미달을 남긴다.

### W0. 기준선과 변경 안전성 확보

1. 보고서 종료 시 `local-camera` FFmpeg가 종료되어 해당 워커가 offline이었다. 두 카메라 송출을 복원·확인한 뒤 같은 장면·해상도·입력 FPS·GUI 창 배치로 측정한다. 1대만 활성인 실행은 인수 자료에서 제외한다.
2. 실제 Compose 프로젝트·파일 순서(GPU·push·CPU override 포함), env 경로, 이미지 ID, GPU 장치, 설정 로드값, 컨테이너 마운트·자원 제한을 기록한다. 최종 C와 다른 항목은 먼저 명시한다.
3. `compose.env`, 운영 `config.yaml`, CPU override, `.wslconfig`, 양쪽 계약 서명 파일 및 전체 운영 데이터의 복귀 사본을 데이터 루트 밖에 보관한다. DB는 [기존 백업 절차](operations.md#운영과-백업)로 일관된 사본을 만들고 전체 파일 사본은 정상 종료 시 확보한다. 복사본에서 무결성·복원을 확인한다.
4. 모델 워밍업과 정상상태를 나누어 30분 기준선을 수집한다. 검증용 진단 호출 자체의 부하가 비교 전후 같도록 한다. 호스트 앱이 적은 구성과 실제 개발 앱 동시 실행 구성을 별도로 기록한다.
5. 녹화·분석·복구 큐의 시작 건수, 기존 실패 2건의 원인·재시도 가능 여부를 확인한다. 과거 실패의 일괄 재등록은 하지 않는다.

**완료:** 재현 가능한 2카메라 기준선과 복원 가능한 자료가 있고, 이후 패치의 변경 전 측정값으로 사용할 수 있다.

### W1. CPU 제한·기동 설정의 배포 통합

대상: [Compose](../server/compose.yml), [GPU Compose](../server/compose.gpu.yml), [ComposeAdapter](../server/operations/compose.py), [계약 서명](../server/deployment/signature.py), 설치·업데이트 경로.

1. 임시 `C:\ProgramData\AI_CCTV\resource-remediation-20261116\compose.cpu-limits.yml`에만 의존하는 상태를 해소한다. 현재 장비에 맞춘 값은 우선 GPU Compose의 기본값 및 기존 env 전달 체계로 통합하고, CPU 전용 배포에 그대로 확산하지 않는다. 별도의 범용 자원 프로필 시스템은 추가하지 않는다.
2. `preprocessing=3.5`, `analysis=1.5`, 기동 60초, 운영 FPS 3.0이 GUI·CLI·업데이트·재기동 경로에서 유지되게 한다. 기존 설치의 FPS 3.0을 보존하며 신규 설치의 기본 FPS 변경은 분리한다.
3. **Compose·AppConfig 변경은 계약 해시를 바꾼다.** 현재 [운영 안내](operations.md)에 따르면 불일치 시 GUI/CLI의 설치 검사·일부 상태 조회도 데이터 초기화로 이어질 수 있다. 운영 관리 도구를 새 코드로 열기 전에, 알려진 이전 계약에서 이번 배포 설정만 바뀐 경우를 처리하는 데이터 보존 업그레이드 경로를 구현·시험한다.
4. 서명을 무조건 재작성하거나 검사를 우회하지 않는다. DB·도메인 계약이 동일함을 확인하고, 예상한 이전/새 배포 해시만 이행시킨다. 미지의 불일치는 운영 원본에 접근하지 않고 별도 복제 환경에서 다룬다.
5. 복제 데이터로 업그레이드·재실행·이전 버전 복귀를 시험한다. DB·영상·설정·시크릿이 유지되고, GPU/push 조합과 시작·중지·조회가 동일 구성을 사용해야 한다. 후보 이미지 준비·시험이 끝난 뒤 임시 override를 제거한다.

**완료:** 자원 제한과 60초 기동 설정이 재배포 후 유지되고, 3회 콜드 스타트에서 두 카메라·모든 모델이 준비된다. 기존 데이터의 자동 초기화가 없고 공통 인수 기준을 통과한다. 실패 시 직전 이미지·Compose·설정·서명을 한 세트로 복원한다.

### W2. 메모리 8GB 시험

1. 현재 `.wslconfig`를 백업하고 `memory`만 `10GB → 8GB`로 바꾼다. `processors=6`, `swap=3GB`, CPU 제한, FPS, 호스트 앱 구성은 고정한다. 기존 다른 키도 보존한다.
2. 유지보수 창에 녹화·복구·서비스를 정상 종료한 뒤 Docker Desktop 종료 → WSL 종료 → Docker Desktop·서비스 기동 순으로 반영한다. 전체 WSL 배포에 영향을 주는 중단 구간을 기록한다.
3. WSL 실제 총 메모리·CPU·swap을 확인하고 모델 로딩 피크, 두 카메라·GUI·신규 분석·복구 동시 부하를 측정한다. 캐시 팽창과 10분 유휴 후 회수도 따로 확인한다.
4. 동일 조건의 10GB 결과와 30분 및 2시간 결과를 비교한다. 자동 회수의 현재 동작부터 관찰하며 주기적 강제 캐시 삭제는 도입하지 않는다.

**채택:** 공통 메모리·기능 기준 통과. **복원:** 새 OOM, 지속 swap/페이지 입출력 증가와 지연 악화, 모델 준비 실패 시 10GB로 되돌리고 유지보수 재기동한다. 8GB에서도 가용 RAM 5GiB를 확보하지 못하면 앱별 실제 점유·서버 피크를 보고하고 W4~W6 후 재평가한다. 6GB 등 추가 축소는 이 계획의 기본 조치가 아니다.

### W3. 녹화 상태 조회의 2초 TTL 캐시

대상: [파일 상태 함수](../server/services/data/app/storage/recordings.py), [Data 카메라 API](../server/services/data/app/api/cameras.py), [녹화 hook](../server/services/data/app/api/recordings.py), [External 녹화 제어](../server/services/external/app/api/cameras.py).

1. `has_active_recording_file()`에 서비스 인스턴스 수명의 작은 캐시를 둔다. 키는 저장 루트와 카메라 ID, 값은 검사 시각·파일 활성 여부로 한정한다. 양성·음성 모두 2초 TTL을 적용하고 단조 시계로 만료를 계산한다. 카메라 삭제·서비스 종료 시 항목을 정리한다.
2. 녹화 시작·종료·카메라 변경이 Data에 반영되는 지점과 segment 완료 hook에서 무효화한다. External 프로세스에서 Data의 로컬 캐시를 직접 지울 수 있다고 가정하지 않는다. 실제 내부 제어 호출 경로에서 무효화하고, hook 유실은 TTL 만료로 재확인한다.
3. 캐시는 파일 존재 상태만 저장한다. 인증·사용자별 카메라 목록·객체 응답은 캐시하지 않는다. 동시 요청의 중복 탐색과 변경 직후 오래된 검사 결과 재삽입을 방지하되 별도 캐시 서버는 도입하지 않는다.
4. 기존 파일 활성 판정(최근 수정 시각·파일 크기)을 유지한다. 캐시 무효화가 녹화 중단을 즉시 파일 비활성으로 바꾼다고 보장하지 않는다. 제어 의도와 실제 파일 상태는 기존 계약대로 구분한다.

**검증:** TTL 내 반복 호출·만료·시작/종료·카메라 삭제/재등록·빈 경로·동시 조회를 테스트한다. `/cameras`·`/cameras/enabled`의 실제 탐색 횟수, hit/miss, CPU 및 HTTP p50/p95를 전후 비교한다. 정상 반복 조회의 탐색 횟수 80% 이상 감소를 작업 목표로 삼고, 상태 갱신이 캐시로 추가 지연되는 시간은 최대 2초로 제한한다. 실패 시 캐시 패치만 되돌린다.

### W4. 관리자 GUI의 반복 변환·조회 절감

대상: [video_worker.py](../server/desktop/admin/video_worker.py)와 화면별 워커 수명 관리.

1. 표시 대상이 아닌 프레임은 RGB 변환·바이트화·`QImage.copy()` 전에 버린다. 실시간 디코더 소비를 임의로 재우지 않고 최신 프레임을 유지한다. 라이브 경로에 먼저 적용하며 녹화 재생의 PTS 기반 동작은 보존한다.
2. 숨김·비활성 영상 창의 표시 변환과 불필요한 객체/이벤트 조회를 중지하고 화면 복귀 시 재개한다. 창 종료로 로컬 송출·중앙 녹화까지 종료되지 않는지 다시 확인한다.
3. 우선 표시·큐 진입 전에 드롭하여 복사를 줄인다. 단순히 디코딩 뒤 프레임을 버리는 것으로 디코딩 비용까지 줄었다고 보고하지 않는다. 프레임 메모리의 수명을 확보하기 전 `QImage.copy()` 자체를 제거하지 않는다.
4. 전역 객체 0.2초·이벤트 0.5초 폴링 간격 변경은 첫 패치에서 제외한다. 활성 화면에서도 요청 중복이 남는 경우에만 후속 변경으로 평가한다.

**검증:** 2개 창 표시/숨김/재개, 장시간 실행, 네트워크 중단·재연결, 객체 좌표와 영상 시각 정합을 확인한다. 디코드·변환·표시·드롭 횟수, GUI CPU/private memory, 큐 깊이·프레임 나이를 비교한다. 영상이 멈추거나 오래된 좌표가 표시되면 해당 패치를 복원한다.

### W5. 저해상도·저FPS 서브스트림

1. 원본 녹화 입력과 분석·다중 미리보기 입력을 분리할 수 있는지 확인한다. 카메라/Edge가 제공하는 서브스트림을 우선 사용한다. 640×360, 5~10fps를 시험 시작값으로 하고 분석 FPS는 3으로 유지한다.
2. 원본은 녹화·고화질 확인용으로 유지한다. 호스트에서 추가 소프트웨어 인코딩을 수행해 서브스트림을 만들면 총 CPU가 늘 수 있으므로 송출 측 비용까지 포함해 채택을 판단한다. 제공이 불가능하면 경로를 보류하고 W6을 현재 해상도에서 평가한다.
3. 분석 프레임 좌표의 스케일·레터박스·종횡비·회전을 원본 좌표로 정확히 변환한다. OSNet/PAR crop과 이벤트·스냅샷에 필요한 화질은 별도 확인한다. 작은 사람·멀리 있는 사람의 탐지 및 속성 품질을 비교한다.
4. 입력 해상도 감소 효과를 먼저 검증한 뒤 하드웨어 디코딩을 별도 후보로 평가한다. 실제 디코더·GPU/VRAM·NVENC 동시 사용을 확인하고 유의미한 추가 절감이 있을 때만 채택한다.

**완료:** 원본 녹화 품질·연속성을 유지하고 좌표 정합·탐지·인물 연결·속성 분석 시나리오를 통과한다. 입력별 디코딩·복사 CPU와 전체 메모리가 줄어야 한다. 실패 시 원본 입력으로 복원한다.

### W6. 남은 탐지 IPC의 공유메모리 전환 — 조건부

대상: [탐지 격리 IPC](../server/services/preprocessing/processors/detection/isolation.py). W5 후에도 IPC가 주요 비용이면 수행한다.

1. 프로세스 격리와 기존 timeout·강제 재시작 동작을 유지한다. 큰 이미지의 Pipe pickle 전송만 카메라별 고정 크기 공유 버퍼와 작은 제어 메시지로 교체한다.
2. 소비 완료 전에 생산자가 덮어쓰지 않도록 소유권·완료 확인을 정의한다. 프레임 ID·크기·dtype을 검증하며 무제한 프레임 큐나 동적 버퍼 누적을 만들지 않는다.
3. 해상도 변경·카메라 해제·자식 timeout/종료·서비스 종료에서 `close/unlink` 책임을 명확히 한다. 부모·자식 RSS 합산보다 공유 영역 포함 실사용과 `/dev/shm` 한도를 함께 본다.

**완료:** 실제 추론 경로의 IPC p50/p95·부모 CPU가 줄고, 자식 장애·재시작 20회 후 공유메모리 누적·프레임 혼선·메모리 손상이 없다. 초기에는 이전 Pipe 경로로 되돌릴 수 있게 변경을 독립시킨다. 서브스트림만으로 공통 목표를 충족하고 IPC 비용이 작으면 도입을 생략한다.

### W7. 복구 작업의 연속 실행 완화

대상: [recover_outages()](../server/services/data/app/workers/recovery.py).

1. 먼저 유효한 복구 작업 10건 이상으로 백로그를 재현한다. 성공/실패 상태 저장 후 다음 claim 전 취소 가능한 1초 휴식을 시험 시작값으로 적용한다. 기존 빈 큐 poll, 실패 재시도 지수 지연, revision·중복 방지·종료 보장은 유지한다.
2. `recovery_poll_interval_seconds`만 늘리는 방식으로 대체하지 않는다. 작업 하나가 길어 API를 계속 압박하면 작업 내부의 처리 예산을 별도 패치로 검토한다.
3. Data 전체에 낮은 CPU 상한을 즉시 적용하지 않는다. 휴식으로도 실시간 API 인수 기준을 충족하지 못할 때만 복구 worker의 별도 서비스 분리를 설계한다.

**완료:** 복구 동시 실행 중에도 API·좌표 지연 기준을 통과하고, 새 실패 없이 고정 백로그가 소진된다. 휴식으로 늘어난 완료 시간을 보고한다. 단순 CPU 감소와 복구 정체를 혼동하지 않는다. 처리 진행이 멈추면 휴식/예산 변경을 복원한다.

### W8. DB·고빈도 스냅샷의 영구 volume 이전 — 조건부

1. 실제 운영 DB·스냅샷·이벤트 이미지·outbox·백업 경로와 접근 서비스를 조사한다. `/snapshots` 벤치마크만으로 실제 DB도 동일 마운트에 있다고 가정하지 않는다.
2. 후보 named volume 또는 WSL Linux 파일시스템에서 기존과 같은 파일 수·WAL·동시 읽기/쓰기 부하를 측정한다. `/tmp`는 영구 저장 후보나 내구성 검증 결과로 취급하지 않는다.
3. DB를 먼저, 스냅샷을 다음 독립 변경으로 이전한다. SQLite 백업·관련 서비스 정상 종료 → 복사 → 무결성·건수·파일 checksum/권한 확인 → 마운트 변경 → 기동 순으로 진행한다. WAL/SHM을 가진 실행 중 DB 파일 하나만 복사하지 않는다.
4. Data·전처리 등 소비자의 동일 경로, UID/GID, 호스트 백업·내보내기·복원 도구 및 설치 패키지 호환성을 맞춘다. Compose 변경은 W1의 계약 보존 절차를 따른다. 초기 단계에서 대용량 원본 녹화 경로는 이전하지 않는다.
5. 이전 원본은 즉시 삭제하지 않는다. 새 저장소에서 쓰기가 발생한 뒤 되돌릴 때는 관련 writer를 멈추고 새 데이터를 일관되게 원래 경로로 역이전한 뒤 구 마운트를 복원한다. 구 DB를 그대로 재사용해 신규 데이터를 잃지 않도록 복귀 리허설을 한다.

**완료:** 로그인·카메라·이벤트·스냅샷·녹화 인덱스·복구·백업 복원이 정상이고, 파일 I/O/API 지연이 개선된다. CPU·메모리도 별도 측정한다. 파일시스템 성능 향상을 곧바로 RAM 절감으로 간주하지 않는다.

## 4. 공통 검증 절차와 기록

1. **패치별 시험:** 대상 동작을 재현하는 테스트를 먼저 준비한다. 배포는 `server/tests/test_operations_compose.py`, `test_contract_signature.py`, 녹화·GUI는 `server/setup/install_helper/tests/test_recording_control.py`, `test_live_video.py`, IPC는 전처리 격리 진단 테스트와 신규 장애 수명 시험을 중심으로 실행한다. 캐시·복구·저장소 변경에는 해당 경계 조건 시험을 추가한다.
2. **실행 환경:** Gemini의 관련 테스트 27개 통과는 기존 기준선이다. 후속 패치 검증으로 대체하지 않는다. `test_storage_paths.py`의 `fastapi` import 실패는 의존성을 갖춘 Data 시험 환경에서 해소해 실행하고, 수집 실패를 통과 처리하지 않는다.
3. **측정 순서:** 한 항목 변경 → 실제 로드값 확인 → 기동/모델 준비 기록 → 5분 예비 관찰 → 30분 동일 부하 A/B → 채택 후보의 2시간 혼합 부하 시험. W2·W6·W8은 유휴 회수·재시작 후에도 자원 점유가 복원되는지 확인한다. 최종 구성은 1일 정상 운영 후 종료/재기동 결과까지 기록한다.
4. **시나리오:** 2카메라+GUI 활성, GUI 숨김/재개, 사람 증가·이동·재등장, 신규 identity/PAR 작업, 복구 백로그, 네트워크 복구, 콜드 스타트를 포함한다. 유지보수·기동 부하는 정상상태 통계와 분리하지만 실패를 누락하지 않는다.
5. **수집:** 호스트 CPU는 3초, 가용 RAM·WSL `used/buff-cache/available/swap`과 컨테이너 CPU·메모리는 10초 간격을 시작값으로 한다. Windows 페이지 입출력, private memory, GPU/VRAM, OOM/restart, API 지연, 좌표 나이·stale, 처리량·큐 대기시간을 같은 시각축으로 남긴다. Docker CPU 100%는 논리 CPU 한 개 기준임을 명시한다.
6. **비교:** API 마이크로벤치마크와 실제 GUI 경유 요청을 구분한다. 동일 장면·카메라 수·사람 수·앱·복구 부하에서 평균/p50/p95·최대와 절대차·상대차를 보고한다. 동시 조건이 다르면 반복 시험한다. 최근 100표본 진단 창을 전체 실행의 p95로 표시하지 않는다.

작업별 결과는 다음 항목으로 남긴다: 소스 revision·이미지 ID, Compose 파일 순서·설정 요약, 카메라/앱/부하 조건, 시작·종료·중단 시각, 원자료 경로, 전후 지표, 테스트 결과, 기능 누락, 채택/복원/보류 이유. 비밀값을 포함한 env·전체 resolved Compose는 일반 보고서에 넣지 않는다.

## 5. 중단·복귀 및 최종 인수

- 새 OOM·모델 준비 실패·메모리 손상·데이터 무결성 오류·녹화 누락이면 해당 시험을 즉시 중단한다. 지연·처리량 기준 미달도 마지막 변경을 복원한 뒤 재측정한다.
- 코드/Compose는 직전 통과 이미지·파일·env·서명 조합으로 복귀하고, `.wslconfig` 변경은 정상 종료 후 재기동으로 반영한다. 패치별 복귀는 최초 조사 상태가 아닌 **직전 통과 상태**를 대상으로 한다.
- 저장소 이전은 W8의 역이전 절차로 신규 쓰기를 보존한다. 데이터 초기화나 `down -v`를 복귀 수단으로 사용하지 않는다.
- 최종 인수는 2카메라·실제 호스트 앱 구성에서 CPU·메모리·기능 기준을 모두 확인한 뒤 수행한다. 한 항목이 미달이면 부분 개선과 미달 조건을 명시한다. 조건부 작업 생략은 남은 병목과 공통 목표 달성 근거로 설명한다.
- 최종 산출물은 채택 패치, 설정·백업/복귀 절차, 운영·배포 문서 갱신 및 후속 검증 결과 보고서다. 현재 보고서의 짧은 안정성 결과만으로 작업 완료를 선언하지 않는다.

## 6. 실행 기록

| 작업 | 상태 | 확인 내용 |
|---|---|---|
| W0 | 보고서 기준선 확보, 현재 호스트 재측정 보류 | Gemini 보고서의 2카메라·최종 C 측정값을 계획 기준선으로 기록했다. 이 실행 환경에서는 Docker CLI를 찾지 못했고 `wsl --status`·배포 목록 조회는 `E_ACCESSDENIED`로 거부되어 실제 Compose·카메라 상태와 복구 자료를 확인하지 못했다. |
| W1 | 대기 | GPU Compose의 정식 통합에는 계약 서명 이행이 필요하다. 그 구현·배포 전후 시험에 필요한 Python 의존성이 현재 환경에 없으며, 변경된 배포 해시가 기존 설치의 데이터 초기화 절차를 잘못 작동시키지 않는지 격리된 설치 자료로 먼저 검증해야 한다. |
| W2 | 대기 | 사용자 `.wslconfig` 파일 접근·WSL 재시작 작업은 실행하지 않았다. W0 백업과 유지보수 조건을 확인한 뒤 별도 적용해야 한다. |
| W3 | 코드 적용 및 단위 검증 완료 | Data 녹화 파일 상태에 2초 process-local TTL을 적용했다. 카메라의 `enabled`·`recording_enabled` 변경, 삭제, 녹화 segment 등록 및 완료 hook에서 해당 캐시를 무효화한다. 캐시 hit·TTL 만료·명시적 무효화·저장 루트 격리를 검증했다. |
| W4~W8 | 대기 | GUI, 디코더/IPC, 복구 백로그, 영구 저장소 변경은 각각의 선행 측정·실제 스트림/볼륨 검증 후 진행한다. |

**W3 변경 파일:** `server/services/data/app/storage/recording_cache.py`, `server/services/data/app/storage/recordings.py`, `server/services/data/app/api/cameras.py`, `server/services/data/app/api/recordings.py`, `server/services/data/test_recording_file_cache.py`.

W3의 단위 시험은 현재 Python에서 3개 통과하고 수정 Python 파일 컴파일도 성공했다. Data API 통합 시험은 현재 Python 환경에 `fastapi`가 없어 import 단계에서 실행할 수 없었다. 계약 해시 변경, `.wslconfig`, 컨테이너 재생성, 데이터 이전은 적용하지 않았다.


## Execution update (2026-11-16)

This update supersedes the earlier execution-status notes above.

- **W0, partial:** User-session read-only inspection found all 6 containers healthy, `cam-001` recording and `local-camera` stopped, so matched 2-camera baseline is pending. `.wslconfig`: 10GB memory, 6 processors, 3GB swap. Windows free RAM was 5.05GiB of 23.89GiB. One instantaneous Docker sample: preprocessing 62.97% CPU/2.764GiB, analysis 0.82%/810.8MiB, data 4.42%/85.38MiB, mediamtx 6.67%/37.21MiB, nginx 3.98%/8.45MiB, external 37.40%/89.24MiB. Do not compare this sample to Gemini's matched 2-camera baseline. Created consistent DB backup `pre_resource_remediation_20261116_132500.db`; preserved original Data image ID `sha256:9145365303d17babb78eb033841acb98a9ae2e3645f587fefe9f8205ec170d5d` under a rollback tag.
- **W1, pending:** Current deployment uses external CPU override `C:\ProgramData\AI_CCTV\resource-remediation-20261116\compose.cpu-limits.yml`. Integrate it into Compose only after contract-signature migration and installation-state rehearsal.
- **W2, pending:** Confirmed `.wslconfig` is 10GB; no edit or WSL restart has occurred. Run the 8GB comparison only after preparing the maintenance procedure.
- **W3, complete:** Added a 2-second process-local TTL for recording-file status and invalidation on camera setting changes/deletion and recording segment creation/completion. Three unit tests passed; a smoke test in the new image passed cache reuse, invalidation, and file rescan. Preserved the DB backup and rollback image, then recreated only Data. All 6 services are healthy; post-restart one-shot Data memory was 51.96MiB.
- Host Python lacks `fastapi`, so host-side API integration tests could not run; the production Data image smoke test passed. Contract-signature changes, `.wslconfig` edits, full WSL restart, and data migration have not been applied.
