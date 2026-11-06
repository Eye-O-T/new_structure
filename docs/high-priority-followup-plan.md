# 고우선순위 잔여 과제 개선 계획

이전 재검토에서 부분 해결로 판정한 회귀 테스트·CI와 추론 실패 진단을 완료한다. 개발 단계의 자동 초기화는 확정된 정책으로 두고, 이번 작업 범위는 인증 테스트의 기대값, 전처리 로그 출력, 두 변경의 회귀 검증으로 한정한다.

| 과제 | 현재 확인된 상태 | 완료 기준 |
| --- | --- | --- |
| 미디어 인증 회귀와 CI | 잘못된 자격증명을 거부하는 동작과 테스트가 기대하는 로그가 다르다. 이전 재검토에서 External 테스트 8개 중 1개가 실패했다. | 인증 거부와 내부 오류를 각각 검증하고, 변경본의 Windows CI 작업이 통과한다. |
| 추론 실패 진단 | 오류 정보가 상태 응답과 로그 레코드의 `extra`에 저장된다. 기본 Uvicorn 설정으로 재현한 로그에는 오류 정보가 출력되지 않았다. | 기본 로그 한 줄에서 카메라 ID, 오류 코드·유형, 실패 단계, 소요시간을 확인할 수 있다. |

## 1. 미디어 인증 회귀 복구

구현은 자격증명 불일치를 HTTP 401로 거부하고 `MEDIA_AUTH_REJECTED`와 `reason=credential_mismatch`를 기록한다. `MEDIA_AUTH_ABORTED`는 카메라 잠금을 획득한 뒤 처리 중 발생한 예상 밖 예외에 사용한다. 현재 테스트가 401 경로에서 두 이벤트를 함께 요구하므로 이 구분에 맞게 기대값을 바로잡는다. [인증 구현](../server/services/external/app/api/media_auth.py) · [회귀 테스트](../server/services/external/tests/test_media_auth_diagnostics.py)

1. 자격증명 거부 테스트에서 HTTP 401, `MEDIA_AUTH_REJECTED`, 거부 사유, 카메라 잠금 해제를 확인한다. 거부 경로에서 `MEDIA_AUTH_ABORTED`가 출력되지 않는지와 요청의 사용자명·비밀번호가 로그에 없는지도 검증한다.
2. 카메라 조회 대역이 내부 예외를 일으키는 테스트를 추가한다. 예외가 호출자에게 전달되고, `MEDIA_AUTH_ABORTED`에 예외 유형이 기록되며, 같은 카메라 잠금이 해제되는지 확인한다.
3. 기존 인증 성공 테스트를 함께 실행해 응답 전송 이후 잠금 해제와 자격증명 비노출을 검증한다. External 테스트 전체가 통과하면 Windows CI 검증으로 진행한다.

변경 대상은 `test_media_auth_diagnostics.py`다. 인증 동작의 기준은 현재 서버 구현을 따른다.

## 2. 기본 로그에 추론 실패 정보 출력

`CameraWorker._record_model_failure()`는 모델 초기화, 추적기 리셋, 추론 실패를 공통 처리한다. 이 함수에서 이미 정제된 값을 로그 메시지 본문에 넣으면 기본 로거와 기존 수집 환경에서 같은 정보를 볼 수 있다. [실패 기록 함수](../server/services/preprocessing/app/pipeline.py) · [컨테이너 실행 설정](../server/services/preprocessing/Dockerfile)

1. 현재 안내 문구에 `camera_id`, `error_code`, `error_type`, `stage`, `elapsed_seconds`를 추가한다. 기존 `extra`와 상태 응답의 값도 유지한다. 측정값을 얻지 못한 경우 `elapsed_seconds=unknown`으로 표시해 0초와 구분한다.
2. 진단 테스트가 로그 레코드 속성과 함께 렌더링된 메시지인 `captured.output`을 검사하도록 보강한다. 초기화·리셋·추론 단계를 확인하고, 시간 초과는 해당 코드와 0 이상 소요시간이 출력되는지 확인한다. 예외 메시지에 넣은 가짜 비밀값과 경로는 출력에 없어야 한다.
3. 개발용 격리 환경에서 실패를 한 건 유도하고 컨테이너의 기본 로그를 확인한다. 같은 실패의 카메라 ID와 코드·유형·단계가 상태 응답과 일치하는지 대조하고, 소요시간은 표시 자릿수를 고려해 비교한다.

출력 예시

```text
inference failed; video monitoring and model retry continue camera_id=cam-001 error_code=GPU_OUT_OF_MEMORY error_type=RuntimeError stage=process elapsed_seconds=0.042
```

변경 대상은 `pipeline.py`와 [추론 진단 테스트](../server/services/preprocessing/tests/test_detection_isolation_diagnostics.py)다. 테스트는 CI가 설치하는 실제 `httpx`를 사용하도록 하고, 새 진단 테스트가 변경본에 포함됐는지 확인한다.

## 3. 검증 순서와 완료 판정

1. Windows에서 Python 3.11과 [CI 설치 단계](../.github/workflows/ci.yml)의 의존성으로 테스트 환경을 준비한다. 저장소 루트에서 두 과제의 테스트를 먼저 실행한다.

   ```powershell
   $env:QT_QPA_PLATFORM = "offscreen"
   $env:PYTHONPATH = ".;lib"
   python -m pytest server/services/external/tests/test_media_auth_diagnostics.py server/services/preprocessing/tests/test_detection_isolation_diagnostics.py -q -rs
   ```

2. 같은 환경에서 Windows CI와 동일한 테스트 범위를 실행한다. `-rs` 출력으로 건너뛴 사유를 확인한다.

   ```powershell
   python -m pytest server/tests server/setup/install_helper/tests server/services/data server/services/external/tests server/services/analysis/tests server/services/preprocessing/tests -q -rs
   ```

3. 변경본의 GitHub Actions `desktop-and-server` 작업 결과와 기본 로그 재현 결과를 기록한다. 두 과제의 테스트가 실제로 수집·실행되어 통과하고, Windows CI 작업이 성공하며, 컨테이너 기본 로그에서 진단 필드 다섯 개가 확인되면 완료로 판정한다. 필요한 의존성 누락으로 건너뛴 테스트는 검증 미완료로 기록한다.
