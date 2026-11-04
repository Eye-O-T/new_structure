# Analysis 실행 후 감지 지연·자원 경합 조사

조사일: 2026-11-04, Asia/Seoul. 아래 시각은 한국 시각이다. 실행 중인 Docker 컨테이너, 읽기 전용 DB 연결, 로그, 기존 카메라 없는 진단 스크립트로 확인했다. 서비스 설정 변경·재시작·카메라 기동·이벤트 생성은 하지 않았다.

## 결론

확인한 범위에서는 Analysis가 GPU·메모리·CPU를 독점해 Preprocessing을 막는다는 증거가 없다. Analysis를 실행한 상태에서도 저장된 사람 이미지의 GPU 감지·추적이 성공했다. 다만 장애 당시의 자원 사용 이력과 실제 모델 예외가 남아 있지 않아, 순간적인 경합이나 당시 추론 실패 원인까지 배제한 것은 아니다.

현재 감지가 실행되지 않는 직접적인 이유는 카메라의 `stream_requested=0`이고 Preprocessing의 `worker_count=0`이기 때문이다. 앞서 송출 중 발생한 추론 실패는 별도 문제다. 식별·분석 작업 대기열은 비어 있다.

## 실행 상태와 자원 측정

| 항목 | 확인 결과 |
| --- | --- |
| Analysis 플러그인 | `server.services.analysis.processors:LocalAppearanceAnalyzer` — PAR/ResNet34가 아닌 CPU 색상·품질 분석기 |
| Analysis GPU 접근 | Docker GPU 요청 없음, `/dev/nvidia*` 없음 |
| Analysis CPU | 첫 `docker stats` 표본 24.83%; 이후 5초 간격 cgroup 증가량 0.48 / 0.83 / 5.88%, 추가 표본 0.47%. 100%는 논리 CPU 1개 사용량 |
| Analysis 메모리 | `docker stats` 약 107~109 MiB. cgroup 전체 메모리는 파일 캐시 등을 포함해 약 261~265 MiB |
| Preprocessing 메모리 | 약 659~728 MiB / 제한 4 GiB, 진단 모델 종료 뒤 표본 |
| GPU | GTX 1650 Max-Q 4 GiB. 진단 전후 사용량 0 MiB, GPU 사용률 0%, compute process 목록 없음 |
| OOM / 재시작 | 두 컨테이너 모두 OOMKilled=false, 재시작 0회. cgroup memory.events의 oom/oom_kill도 0 |
| 작업 상태 | identity 8건, analysis 8건 모두 complete. pending/running 없음 |
| 이벤트 전달 | outbox pending/rejected/waiting 모두 0 |
| Docker VM | 논리 CPU 8개, 메모리 약 11.63 GiB. 측정 당시 Linux MemAvailable 약 9.86 GiB, swap 사용 약 4 MiB |
| VM 압박 지표 | 15초 측정에서 CPU PSI avg10=0.00%, memory/io PSI avg10=0.00% |

작업자는 한 번에 작업 하나만 처리하며 빈 대기열은 약 1초마다 조회한다. Analysis는 모델·스냅샷을 읽기 전용으로 마운트하고 DB 파일을 직접 열지 않는다. identity와 analysis는 서로 다른 stage의 작업을 임대하므로 Analysis가 식별 작업을 가져가지는 않는다.

공유 Data API/SQLite는 간접 경합 지점이다. 빈 claim도 `BEGIN IMMEDIATE` 트랜잭션을 사용하지만, 현재 DB는 약 412 KiB, 작업은 총 16건, WAL은 0바이트이고 조회 범위에서 DB 잠금 오류는 없었다. 이 경로가 장애의 원인이라는 근거는 찾지 못했다.

## 실행 전후 시각 대조

| 시각 | 관찰 |
| --- | --- |
| 13:37:40.589 → 13:37:51.398 | Analysis 시작 전 첫 출현 이벤트 8의 발생 → 저장. 이미 약 10.809초 지연이 있었다 |
| 13:38~13:39 | 추가 출현 이벤트 생성·식별 완료. 마지막 identity 완료는 13:39:41.353 |
| 13:39:40.663 | 첫 시험의 MediaMTX 송출 연결 종료 |
| 13:39:41.941 / 13:39:44.194 | 송출 종료 이후 Preprocessing RTSP DESCRIBE 401. Analysis 시작 전에도 발생 |
| 13:41:42.275 | Analysis 컨테이너 시작 |
| 13:41:46.964 | 밀린 Analysis 8건의 마지막 완료. 이후 분석할 대기 작업 없음 |
| 13:42:17.397 | 두 번째 시험의 송출 시작 |
| 13:42:41.451 / 13:43:42.912 | Preprocessing `inference failed; video monitoring and model retry continue` |
| 13:44:20.814 | 카메라 설정 갱신. 조회 당시 `stream_requested=0`, `recording_enabled=0` |
| 13:44:21.138 | 두 번째 송출 연결 종료 |
| 13:44:22.469 | 송출 종료 뒤 RTSP 401 |
| 13:44:23.140 | 모델 준비 실패/재시도 로그. 카메라 중지와 인접하므로 이것만으로 독립적인 초기화 장애라고 단정하지 않음 |

401은 두 시험 모두 송출 종료 뒤 발생했다. 특히 두 번째 시험의 앞선 추론 실패를 401만으로 설명할 수 없다. DB의 `status=online`만 보면 오해할 수 있으며, 실제 감지 대상 선정은 `stream_requested`도 확인한다.

## Analysis를 켜 둔 상태에서 실제 GPU 추론

기존 `server/tools/diagnose_detection_latency.py`를 운영 Preprocessing 컨테이너의 임시 파일로 실행하고 삭제했다. 실제 카메라는 열지 않았고, 두 번째 입력은 기존 이벤트 17의 원본 스냅샷이다. 운영과 같은 YOLO 파일·cuda:0·초기화 제한 30초·추론 제한 10초를 사용했다.

| 입력 / 실행 | 모델 준비 | 첫 프레임 | 다음 두 프레임 | 검출 수 |
| --- | ---: | ---: | ---: | ---: |
| 검은 1280×720 / 격리 | 4.952초 | 3.278초 | 0.038 / 0.039초 | 0 |
| 검은 1280×720 / 직접 | 3.701초 | 3.221초 | 0.030 / 0.023초 | 0 |
| 저장된 사람 이미지 / 격리 | 4.859초 | 3.253초 | 0.047 / 0.049초 | 2 |
| 저장된 사람 이미지 / 직접 | 3.634초 | 3.340초 | 0.031 / 0.027초 | 2 |

이 시험은 Analysis 실행 상태에서 GPU 감지·추적이 가능함을 보여 준다. 실시간 RTSP·프레임 전달·좌표 발행·UI까지의 지연이나 장애 당시의 시간초과를 재현한 결과는 아니다. 진단 뒤 GPU 메모리 점유는 0 MiB로 돌아왔고 서비스 상태와 이벤트 대기열은 유지됐다.

## 남은 원인 후보와 조치 우선순위

1. **Preprocessing 초기 추론/재준비 경로를 우선 조사한다.** 실제 설정은 `DETECTION_MODEL_TIMEOUT_SECONDS=10`, startup=30, retry=30초다. YOLO 생성 직후 ready를 보내고 첫 GPU 추론 준비는 첫 프레임에서 수행한다. 이번 시험도 첫 프레임이 후속 프레임보다 수십 배 느렸다. 다만 이번에는 10초 안에 성공했으므로 당시 실패를 시간초과로 확정할 수 없다.
2. **실제 오류를 보존해야 한다.** 감지 자식은 process 예외를 `kind=error`로 축약하고 부모는 `DETECTION_MODEL_FAILED`로 바꾼다. pipeline의 오류 코드는 기본 로그에 보이지 않고 작업자 제거 시 상태도 사라진다. 정제된 오류 유형·코드·실패 소요시간과 중지된 작업자의 최근 진단을 남기는 것이 원인 확정의 우선 작업이다.
3. **초기 준비와 실시간 추론을 구분한다.** 실제 GPU warmup 완료 뒤 ready를 알리고 추적 상태를 초기화하는 개선을 검토한다. 첫 좌표는 관측 시각 기준 3초 후 만료되므로, 준비 후 최신 프레임을 선택하는 경로도 함께 확인한다. 제한 시간이나 좌표 유효기간을 일괄 확대하는 근거는 없다.
4. **호스트 여유도는 별도 확인 대상이다.** Windows의 한 표본은 CPU 부하 59%, 물리 메모리 약 23.9 GiB 중 가용 2.5 GiB, 가상 메모리 잔여 약 2.0 GiB였다. WSL 작업 집합은 약 10.9 GiB였지만 Linux 내부 가용 메모리가 크므로 이를 컨테이너 메모리 누수로 해석하지 않는다. 같은 표본에서 SearchIndexer·Defender 등의 CPU 사용도 관찰됐다. 측정에는 진단/WMI 조회 부하도 포함되며 장애 시점의 원인이라는 증거는 아니다.
5. **Analysis 제한 부재는 잠재적 경합 조건이다.** 현재 Analysis는 CPU·메모리·PID 제한이 없고 기본 CPU 가중치를 사용한다. Preprocessing은 cpu_shares=512다. CPU 포화 시 Preprocessing이 상대적으로 불리할 수 있으나 현재 지속 포화는 관찰되지 않았다. 향후 PAR로 전환할 때 CPU 스레드 수·컨테이너 상한을 함께 검토할 대상이며, 현재 장애 원인으로 단정하지 않는다.

이번 조사는 운영 설정과 소스를 수정하지 않았다. 앞선 초기 추론 조사 내용은 [Preprocessing 지연 조사](preprocessing-latency-investigation-2026-11-04.md)도 참고한다. 해당 문서의 카메라·이벤트 상태는 이번 시험보다 이전 시점의 관찰이다.
