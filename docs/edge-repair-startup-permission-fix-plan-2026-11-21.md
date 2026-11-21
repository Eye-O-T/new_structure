# Edge Repair 상태 검사 권한 오류 수정 계획서

- 작성일: 2026-11-21
- 대상: 현재 저장소의 Edge CLI, Repair 복원, systemd 시작 검사 및 Debian 패키지
- 근거: 사용자가 제공한 Pi `bull-sa-jo`의 2026-11-21 로그와 현재 코드
- 상태: 원인 확인 및 수정 계획 작성 완료. 이 문서 작성 시 구현 코드·Pi 설정·서비스는 변경하지 않음
- 목표: Repair 백업 보호 권한을 유지하면서 일반 서비스 계정의 정상 기동을 복구하고, 미완료 Repair 중에는 안전하지 않은 기동을 차단

login 방법
ssh phoenix@bull-sa-jo.local
password: qwer

## 1. 확인된 장애와 성공 기준

Pi 로그에서 네트워크 정책 유닛은 16:29:22에 정상 완료했다. 이어 캡처·관리 서비스가 기동했지만 16:29:27부터 다음 오류로 종료와 재시작을 반복한다.

```text
if (REPAIR_ROOT / "journal.json").exists():
PermissionError: [Errno 13] Permission denied:
'/var/lib/ai-cctv-edge/repair/journal.json'
```

운영 서비스는 `User=ai-cctv-edge`로 실행되고, `repair_lock()`과 Debian `prerm`은 Repair 디렉터리를 root 소유 `0700`으로 만든다. 일반 서비스 계정은 이 디렉터리를 탐색할 수 없어 파일 존재 여부 검사부터 실패한다. `journal.json`이 실제 존재하는지는 이 오류만으로 판단할 수 없다.

성공 기준은 다음과 같다.

1. Repair 디렉터리가 root 소유 `0700`인 기존 장치에서 캡처·관리·복구 서비스가 정상 기동한다.
2. 일반 서비스 계정은 private journal과 백업을 읽거나 변경할 수 없다.
3. 미완료·손상된 Repair 상태에서는 설정 검증 없이 서비스가 시작되지 않는다.
4. 정상 Repair 완료 및 실패 복원 과정에서 종전에 실행 중이던 서비스를 다시 올릴 수 있다.
5. 새로운 실행 인스턴스의 캡처·관리 상태를 확인하고, 명시적인 송출 요청 후 서버의 실제 영상 수신을 확인한다.

## 2. 증거 해석과 범위

| 제공된 증거 | 해석 |
|---|---|
| `ai-cctv-edge-network.service` 정상 완료 | 이전의 NAMESPACE·nft 문법 오류는 이번 로그의 직접 중단 원인이 아님 |
| 캡처 `run`과 `serve-control`에서 동일한 PermissionError | 공통 시작 검사에 있는 권한 경계 결함이 직접 원인 |
| `serve-recovery`에도 같은 `.exists()` 코드 존재 | 제공 로그에 복구 유닛 오류가 없어도 같은 조건의 회귀 검증 필요 |
| `rpicam-hello`에서 `imx708_wide` 열거 | 센서 인식은 확인됨. 캡처·인코딩·송출 성공까지 입증하지는 않음 |
| runner 상태 갱신 시각이 16:15, 장애 로그는 16:29 이후 | 이전 프로세스가 남긴 상태이므로 현재 프로세스의 성공·실패 판정에 사용하지 않음 |
| publisher 오류가 11월 20일 기록 | 이번 장애의 RTSP 실패 원인으로 채택하지 않음 |
| 저장된 `stream_enabled`, `recording_enabled`가 모두 false | 기동 복구와 송출 요청을 별도로 확인해야 함 |
| `ConfigurationDirectory` 0750/0755 경고 | 이 로그에서 종료를 일으킨 예외는 아님. 필요 시 유닛 권한 선언 정합성만 함께 조정 |

수정 범위는 시작 권한 경계와 그에 직접 연결된 Repair·복원 경합으로 제한한다. 카메라 파이프라인, RTSP 자격 증명, 서버 등록·DB, 송출·녹화 의도 정책 변경은 포함하지 않는다. 앞서 수정한 nftables 호환성·런타임 디렉터리 생성은 유지하고 회귀 여부만 확인한다.

## 3. 수정 방침

### 3.1 private 상태와 일반 서비스의 기동 판단 분리

private 데이터는 기존 경로와 보호 수준을 유지한다.

| 경로 | 소유권·권한 | 접근 주체 |
|---|---|---|
| `/var/lib/ai-cctv-edge/repair/` | root:root, `0700` | root 복원·패키지 처리 |
| private journal·잠금·백업 | root 소유, 파일 `0600`, 백업 디렉터리 `0700` | root 전용 |
| `/run/ai-cctv-edge-network/` | root:ai-cctv-edge, `0750` | root 쓰기, 서비스 계정 탐색·읽기 |
| 신규 `startup.json` | root:ai-cctv-edge, `0440` | root 작성, 서비스 계정 읽기 전용 |

root가 private journal·패키지 전환·실제 네트워크 정책을 검사하고, 일반 서비스는 비밀을 포함하지 않는 기동 허용 상태만 읽는다. `startup.json`의 최소 필드는 형식 버전, 허용 여부, 허용 진입점, config 내용의 digest, 현재 부팅 식별자다. 내부 Repair 재시작에 필요한 작업 ID는 비밀 데이터 없이 식별 목적으로만 포함한다. 토큰·게시 비밀번호·백업 내용·원본 journal 전체를 복제하지 않는다.

파일은 같은 디렉터리에서 임시 작성 → 권한·소유권 설정 → 원자적 교체 순서로 게시한다. 서비스 계정에는 상위 디렉터리 쓰기 권한도 주지 않는다. 재부팅 후 상태가 없으면 root 사전 검사에서 재생성한다.

### 3.2 root 사전 검사와 일반 계정 검사

root용 내부 CLI `verify-startup --entrypoint <진입점>`을 추가한다. 세 운영 유닛의 기존 root `ExecStartPre=+... verify-network-policy`를 이 명령으로 연결하고, 내부에서 기존 네트워크 정책 검증도 수행한다. 같은 검사를 중복 호출하는 구조는 피한다.

root 검사 순서는 다음과 같다.

1. 설정 및 진입점, 패키지 전환 상태, private Repair 상태를 검사한다.
2. 시작이 허용되는 상태인지 판정한다. 이 함수가 private 복원이나 설정 덮어쓰기를 암묵적으로 수행하지 않는다.
3. 기존 `verify_persistent()`로 실제 정책과 설정 일치를 검증한다.
4. 모든 조건을 통과한 경우에만 해당 config·부팅·진입점의 기동 허용 상태를 게시한다.
5. 거부·검증 실패 시 기존 허용 상태를 차단 상태로 전환하고 명확한 비정상 종료 코드를 반환한다.

일반 계정의 `run`, `serve-control`, `serve-recovery`는 private 디렉터리를 직접 조회하지 않는다. 공통 검사 함수에서 `startup.json`의 허용 여부·진입점·config digest·부팅 식별자와 기존 네트워크 정책 상태를 확인한다. 서비스 계정으로 직접 CLI를 실행해도 같은 검사를 거친다.

일반 계정은 기동 상태를 새로 만들거나 허용으로 바꾸지 않는다. 파일 없음·손상·권한 오류·config 불일치는 명확한 기동 거부로 처리한다. `PermissionError`를 잡아 정상 상태로 간주하는 수정은 하지 않는다.

### 3.3 구현 위치와 최소화

root 판정과 일반 계정 검사는 `repair.py`의 시작 검사 함수 또는 작은 `startup.py`에 모은다. 두 구현을 병행하지 않는다. CLI·Repair·네트워크 정책 사이의 순환 import를 피하도록 import 방향을 먼저 정한다. 별도 모듈을 쓰는 경우에는 경로·상태 직렬화·검사만 담당하게 하고 서비스 제어 프레임워크로 확장하지 않는다.

`repair_lock()`의 권한 제한을 완화하거나 운영 서비스를 root로 바꾸지 않는다. `sudo`를 일반 서비스의 실행 중 동작에 추가하지 않는다. CLI에서 private `PACKAGE_TRANSITION.exists()`를 조회하는 경로도 같은 권한 기준으로 점검한다. 관리용 변경 명령에는 root 요구를 먼저 적용해 private 파일 접근 예외가 사용자 오류 안내를 대신하지 않게 한다.

## 4. Repair·복원 중 서비스 재시작 계약

단순히 세 `.exists()` 줄만 제거하면 미완료 작업의 기동 차단이 약해진다. 반대로 root 사전 검사에서 journal 존재만으로 모두 거부하면 Repair가 서비스를 복구하려는 단계도 차단된다. 다음 상태를 구분한다.

| 상황 | root 사전 검사 결과 |
|---|---|
| journal 없음, 설정·정책 정상 | 정상 기동 허용 |
| `PREPARED`, `SERVICES_STOPPED`, `PAIRING`, `APPLYING` | 일반 기동 거부 |
| `LOCAL_CONFIGURED` | 후보 파일·정책 검증 완료 및 현재 Repair 소유자의 명시적 내부 재시작 허가가 있을 때만 허용 |
| `ROLLING_BACK` | 이전 파일·정책 복원 완료 및 복원 소유자의 명시적 내부 재시작 허가가 있을 때만 허용 |
| `RECOVERY_REQUIRED`, journal 손상·읽기 실패 | 기동 거부, root 복원 경로로 해결 |
| `COMMITTED`, `ROLLED_BACK` | 확정된 파일·정책을 검증한 뒤 허용. 잔여 백업 정리와 혼동하지 않음 |
| 재부팅 후 미확정 journal 잔존 | 먼저 기존 recovery 유닛으로 복원. 이전 부팅의 내부 허가로 시작 금지 |

내부 재시작 허가는 root 전용 `/run` 파일로 제한하고, 작업 ID·허용 유닛·현재 부팅과 작업 소유 프로세스의 일치를 검사한다. journal의 단계 이름만 맞는 것으로 허가하지 않는다. 다른 작업이 잠금을 점유한 경우와 실제 잠금 소유자가 실행하는 정상 재시작을 구분해야 한다. 필요한 소유자 정보는 root 잠금 획득 시 기록한다.

Repair는 파괴적 단계와 서비스 중지 전에 일반 기동 상태를 차단한다. 파일·정책이 검증된 기동 단계에서만 필요한 진입점의 내부 허가를 발급하고, 기동 확인 뒤 제거한다. 중단·실패 시 이전 허가를 무효화한다. `/run` 허가가 없어지는 재부팅은 기존 영구 journal 복원을 따른다.

현재 `recover_pending()`은 넓은 `RuntimeError`를 잡고 단계만으로 건너뛰는 경로가 있다. 이를 시작 허용 판단의 근거로 사용하지 않도록 정리한다. 실제 복원 실패를 잠금 경합으로 오인하지 않게 예외 종류와 처리 범위를 구분한다.

`recover-repair --defer-services`에서는 파일·정책 복원만 수행하고 systemd의 후속 시작 순서를 따른다. 자신을 선행 조건으로 기다리는 운영 유닛의 기동을 동기 호출하지 않는다. Repair 소유자가 직접 복원하는 경우에는 검증된 내부 허가로 기존 active 유닛 집합을 복구한다.

패키지 전환도 확인한다. 현재 `postinst`는 서비스를 올린 뒤 `package-transition`을 지운다. 새 시작 검사가 이 파일을 무조건 거부하면 패키지 자체의 기동까지 실패하므로, 파일·wheel·유닛 배치와 설정 처리가 끝난 뒤 전환 가드를 정리하고 정상 시작 검사를 거쳐 서비스를 올리는 순서로 맞춘다. 파일 변경 구간은 기존 작업 잠금으로 보호하고, 전환 완료 전 시작 허가를 게시하지 않는다. 실제 기동 실패는 패키지 오류로 보고하되 보호된 Repair 백업을 삭제하지 않는다.

## 5. 수정 파일과 작업 순서

| 순서 | 대상 | 변경 및 확인 |
|---|---|---|
| A | `edge/tests/` | root `0700` 부모 아래 journal을 일반 UID로 조회하는 재현 시험 작성. 현재 코드의 실패 확인 |
| B | `repair.py`, 필요 시 신규 `startup.py` | private 검사·읽기 전용 기동 상태·내부 재시작 허가 구현 |
| C | `cli.py` | 내부 `verify-startup` 연결, 세 서비스의 직접 journal 검사 교체, 관리 명령의 권한 검사 순서 정리 |
| D | `edge/systemd/` | root 사전 검사 연결, 기존 서비스 UID 유지, recovery 유닛과 시작 순서 확인 |
| E | `repair.py`, 필요 시 `network_policy.py` | 시작 상태 차단·재발급을 기존 잠금·복원·정책 검증과 연결 |
| F | Debian `prerm`·`postinst`·`verify_deb.sh` | 패키지 전환과 기동 상태의 일관성, 새 모듈·유닛 포함 검증 |
| G | Edge 테스트와 Actions | 비root Linux 권한 재현·회귀 검증을 새 `.deb` 생성 전에 실행 |
| H | `edge/README.md`, `docs/operations.md` | 적용 후 서비스 확인과 별도 송출 요청 절차 보완 |

`ConfigurationDirectoryMode=0750` 선언을 추가한다면 기존 패키지의 디렉터리 권한과 맞추는 변경으로 한정한다. 이를 PermissionError의 근본 수정으로 보고하지 않는다.

## 6. 검증 계획

### 6.1 자동 검증

| ID | 조건 | 합격 기준 |
|---|---|---|
| T01 | Linux root `0700` Repair 디렉터리, journal 없음 | 일반 UID의 서비스 진입이 private 경로를 조회하지 않고 정상 통과 |
| T02 | 같은 디렉터리에 미완료 journal 존재 | root 사전 검사 거부, 일반 UID는 보호 데이터 접근 없이 명확히 기동 거부 |
| T03 | journal 손상·부재·읽기 실패, 시작 JSON 없음·손상·구형 config·다른 부팅 | 실패를 정상으로 간주하지 않음. journal 부재와 private 경로 접근 불가를 구분 |
| T04 | 서비스 UID로 journal·백업·잠금 및 기동 JSON 쓰기 시도 | 접근 거부. 기동 JSON 읽기만 허용 |
| T05 | 정상 Repair의 `LOCAL_CONFIGURED`와 서비스 재시작 | 현재 소유자의 허가만 유효, 기존 active 집합 복구 후 COMMITTED |
| T06 | Pairing·쓰기·정책 실패 뒤 `ROLLING_BACK` | 이전 파일·정책 검증 후 서비스 복원, 이전 내용과 파일 존재 상태 보존 |
| T07 | 허가 발급 전후 강제 종료·부팅 변경·stale 허가 | 미확정 신규 설정 자동 실행 금지, 기존 복원 경로 통과 |
| T08 | `run`, `serve-control`, `serve-recovery` 각각 직접 실행 및 systemd 경로 | 동일한 시작 판단, root 운영 전환·private 접근·거짓 성공 없음 |
| T09 | Repair와 패키지 업그레이드·설정 변경 경합 | 기존 잠금 유지, 과거 허용 상태 재사용 금지, 자기 교착 없음 |
| T10 | `COMMITTED`·`ROLLED_BACK` 이후 정리 실패와 재실행 | 확정된 파일 유지, 기동 가능 여부와 잔여 정리 결과 구분 |

Windows에서 `Path.exists()`만 mock한 테스트로 T01·T04를 통과 처리하지 않는다. Linux 임시 경로에 실제 UID·GID와 `0700`·`0600`을 적용하고, 별도 프로세스의 UID/GID를 내려 재현한다. 테스트는 실제 장치의 `/var/lib`를 수정하지 않으며 정리 시 테스트 경로만 사용한다.

기존 `test_repair.py`, `test_network.py`, `test_network_policy.py`도 실행한다. Linux 권한 시험을 실행하지 않은 빌드를 배포 검증 완료로 표시하지 않는다. Actions의 DEB 작업은 이 회귀 시험 실패 시 artifact 생성·배포를 진행하지 않도록 연결한다.

### 6.2 Pi 인수시험

1. 현재 config·인증 파일·녹화·상태를 보존하고 패키지·config의 버전과 파일 식별 정보를 기록한다.
2. 수정 커밋에서 생성된 새 `.deb`를 설치한다. 같은 버전이면 `apt install --reinstall`로 적용하고 build-info의 소스 revision을 확인한다.
3. `daemon-reload`와 서비스 시작 결과를 확인한다. `systemctl is-active` 한 번만으로 성공 판정하지 않고 최소 60초 동안 반복 종료 여부·PID·재시작 횟수를 관찰한다.
4. 새 `runner_instance_id`·PID·갱신 시각으로 status가 갱신되는지, 관리·복구 API가 응답하는지 확인한다. 오래된 JSON의 `stopped`·`offline`을 현재 상태로 사용하지 않는다.
5. 저장된 송출·녹화 요청이 false인 조건에서는 서비스 정상 기동과 송출 대기를 합격 조건으로 삼는다. 수정 코드가 임의로 요청을 true로 바꾸지 않아야 한다.
6. 별도 운영 시험에서 Server GUI로 송출·녹화를 요청한 뒤 새 publisher 상태, 서버 실제 영상, 로컬 녹화 갱신을 확인한다. 새 RTSP 오류가 있으면 그 시각의 로그로 후속 조사한다.
7. 정상 재시작·재부팅, 미완료 Repair의 부팅 복원, Repair 실패 후 기존 서비스 재기동을 각각 시험한다. 백업·토큰에 일반 계정 접근 권한이 생기지 않았는지 확인한다.

패키지 재설치와 권한 오류 수정 자체는 재페어링을 요구하지 않는다. 이번 적용에서 ID·NIC·서버·토큰을 바꾸지 않는다는 전제다. 기존에 다른 등록·주소 문제가 있으면 별도 장애로 기록한다.

## 7. 완료 조건과 복구 원칙

- [ ] 로그의 PermissionError를 Linux 실제 권한으로 재현하고 수정 후 해소했다.
- [ ] 세 운영 서비스가 일반 계정으로 안정 실행되고 최신 상태를 기록한다.
- [ ] private Repair 데이터의 root 전용 권한을 유지했다.
- [ ] 미완료 작업 차단과 정상 Repair·복원의 내부 재시작이 모두 검증됐다.
- [ ] 패키지 설치·업그레이드·재부팅 검증을 통과했다.
- [ ] 송출 요청이 꺼진 대기 상태와 실제 요청 후 영상 수신을 별도로 확인했다.
- [ ] 자동 시험·Pi 인수시험의 통과·미실행·추가 오류를 구분해 기록했다.

실패 시 설정·토큰·녹화·복원 백업을 보존하고, 기동 허가를 차단한 상태로 원인을 기록한다. private 디렉터리를 임의로 공개하거나 journal을 삭제해 기동시키는 절차는 제공하지 않는다. 이전 패키지로 되돌리더라도 그 패키지에 동일한 기동 결함이 없는지 먼저 확인한다.

## 8. 코드 근거

- [서비스 진입점과 private journal 검사](../edge/src/ai_cctv_edge/cli.py)
- [Repair 디렉터리 권한·잠금·복원·서비스 재기동](../edge/src/ai_cctv_edge/repair.py)
- [root 정책 검증과 서비스 계정용 상태](../edge/src/ai_cctv_edge/network_policy.py)
- [캡처 유닛](../edge/systemd/ai-cctv-edge.service), [관리 유닛](../edge/systemd/ai-cctv-edge-control.service), [복구 유닛](../edge/systemd/ai-cctv-edge-recovery.service)
- [미완료 Repair 복원 유닛](../edge/systemd/ai-cctv-edge-repair-recovery.service)
- [패키지 전환 잠금](../edge/packaging/debian/prerm), [패키지 설치](../edge/packaging/debian/postinst), [패키지 검증](../edge/packaging/verify_deb.sh)
- [기존 Repair 회귀 시험](../edge/tests/test_repair.py), [DEB 빌드 Actions](../.github/workflows/build-edge-deb.yml)
