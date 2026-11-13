# Edge DEB 빌드 실패 수정 계획서

작성일: 2026-11-13 (Asia/Seoul)

상태: 워크플로 수정 및 로컬 검증 완료. 수정 커밋의 GitHub Actions 실행과 ARM64 DEB artifact 검증은 저장소에 푸시한 후 수행해야 함.

## 1. 결론과 조사 범위

현재 워크플로는 MediaMTX v1.9.0에서 제공하지 않는 `checksums.sha256`을 다운로드한다. 이것이 이미지의 HTTP 404 및 curl 종료 코드 22를 설명한다. ARM64 압축 파일 다운로드는 성공했으며, DEB 생성 단계 전에 중단된 것으로 판단된다.

공식 릴리스 자산의 부재는 GitHub API로 확인했다. 실패한 Actions 실행 ID와 전체 로그는 확보하지 못했으므로, 이미지 속 실행이 아래 로컬 워크플로와 동일하다는 전제로 실패 명령을 특정했다.

- 조사한 로컬 HEAD: `0ede847dc993fa344170b9d22f1b5e3538d15f37`
- 조사 대상: `.github/workflows/build-edge-deb.yml`, `edge/packaging/build_deb.sh`, `edge/packaging/verify_deb.sh`, `edge/README.md`
- 작업 시작 시 `edge/README.md`에 기존 미커밋 변경이 있었다. 이번 문서 작성에서 이 파일은 변경하지 않았다.

## 2. 확인한 근거

### 공식 배포 상태

[공식 v1.9.0 릴리스 API](https://api.github.com/repos/bluenviron/mediamtx/releases/tags/v1.9.0)를 조회해 다음을 확인했다.

| 항목 | 확인 결과 |
|---|---|
| 릴리스 | `v1.9.0`, 공개된 정식 릴리스 |
| 업로드된 배포 자산 | 7개, 모두 플랫폼별 압축 파일 |
| Linux ARM64 자산 | `mediamtx_v1.9.0_linux_arm64v8.tar.gz` |
| ARM64 자산 ID | `188400714` |
| ARM64 자산 크기 | `17,714,562` bytes, 이미지의 약 `16.8M`과 부합 |
| `checksums.sha256` | 자산 목록에 없음 |
| 다른 이름의 별도 체크섬 자산 | 자산 목록에 없음 |
| ARM64 자산 API `digest` | `null` |

업로드 자산 7개의 이름은 다음과 같다. GitHub가 별도로 표시하는 소스 코드 다운로드는 이 목록의 업로드 자산과 구분한다.

```text
mediamtx_v1.9.0_darwin_amd64.tar.gz
mediamtx_v1.9.0_darwin_arm64.tar.gz
mediamtx_v1.9.0_linux_amd64.tar.gz
mediamtx_v1.9.0_linux_arm64v8.tar.gz
mediamtx_v1.9.0_linux_armv6.tar.gz
mediamtx_v1.9.0_linux_armv7.tar.gz
mediamtx_v1.9.0_windows_amd64.zip
```

따라서 체크섬 파일명을 추측해서 교체하거나, 이 자산의 API `digest`를 기대값으로 사용하는 방식은 현재 확인한 배포 상태에서 해결책이 되지 않는다.

### 로컬 코드와 이미지의 대응

| 위치 | 현재 동작 | 이미지와의 대응 |
|---|---|---|
| 워크플로 54–57행 | ARM64 압축 파일 다운로드 | `100 16.8M` 완료 |
| 워크플로 58–60행 | `v1.9.0/checksums.sha256` 다운로드 | 다음 curl 요청에서 HTTP 404 |
| 워크플로 62–64행 | 체크섬 파일에서 기대값을 읽어 압축 파일 검증 | 앞선 curl 실패로 미도달 추정 |
| 워크플로 76행 이후 | `build_deb.sh` 실행 | 다운로드 단계 실패로 미도달 추정 |

## 3. 수정 방침

MediaMTX v1.9.0과 ARM64 대상을 유지하고, 검토한 압축 파일의 SHA-256을 워크플로에 상수로 고정한다. 정상 실행에서는 다운로드한 파일을 이 고정값과 비교한 뒤에만 압축 해제와 바이너리 실행을 허용한다.

압축 파일 검증 후 추출된 `mediamtx`의 SHA-256을 계산해 기존 `MEDIAMTX_SHA256`으로 전달한다. 이 값은 압축 파일의 해시와 다른 값이다. 기존 빌더 및 패키지 검사 스크립트의 바이너리 검증 계약을 유지한다.

정상 경로는 다음과 같다.

```text
공식 ARM64 압축 파일 다운로드
  → 저장소에 고정한 압축 파일 SHA-256과 비교
  → 압축 해제
  → ARM64에서 MediaMTX 버전 확인
  → 추출 바이너리 SHA-256을 MEDIAMTX_SHA256으로 전달
  → 기존 DEB 빌드 및 패키지 검사
  → DEB 체크섬 검사 및 artifact 업로드
```

다운로드 직후 계산한 해시를 같은 다운로드의 기대값으로 사용하지 않는다. 체크섬 실패를 무시하는 `|| true`, 검증 생략, 검증 실패 시 자동 진행도 추가하지 않는다.

## 4. 구현 순서와 완료 기준

### 단계 1 — 최초 기준 해시 확보

1. 네트워크 사용이 가능한 환경에서 다음 공식 HTTPS 주소의 파일을 별도 작업으로 다운로드한다.

   ```text
   https://github.com/bluenviron/mediamtx/releases/download/v1.9.0/mediamtx_v1.9.0_linux_arm64v8.tar.gz
   ```

2. `sha256sum`으로 압축 파일의 64자리 SHA-256을 계산한다. 다운로드 URL, 자산 ID, 크기, 확보 일시와 계산 결과를 변경 설명에 기록한다. 계산 결과는 `4542af4acdea9948ce6cbb662b39896915339159420c50bcb7ed37caba1d96d7`이다.
3. 압축 내부 구성과 ARM64 바이너리 여부를 확인하고 ARM64 환경에서 `--version` 결과가 `v1.9.0`인지 확인한다. 기존에 별도로 검증해 보관한 동일 배포 파일이 있다면 해시도 대조한다.
4. 확인한 압축 파일의 SHA-256을 워크플로의 고정 기대값으로 사용한다. 완료.

완료 기준: 실제 파일에서 계산한 SHA-256과 확보 근거가 기록되어 있고, 구현에 임시값이나 미확인 해시가 남아 있지 않다. 달성.

제약: 공식 릴리스에 별도 체크섬이 없고 API `digest`도 비어 있어, 이 절차는 최초 확보한 파일을 기준으로 이후 변경을 탐지하는 방식이다. 공식 서명이나 독립적인 공식 체크섬으로 최초 파일의 진위를 입증한 것으로 표현하지 않는다. 버전 문자열과 파일 크기만으로도 진위를 보증할 수 없다.

실제 공식 HTTPS 자산을 임시 폴더에 다운로드해 SHA-256을 계산하고 검사한 뒤 임시 파일을 제거했다. 다운로드 파일 크기는 `17,714,562` bytes이며, 압축 파일은 `mediamtx`, `mediamtx.yml`, `LICENSE`를 포함한다. 다운로드한 바이너리의 ARM64 실행과 버전 확인은 ARM64 GitHub Actions runner에서 최종 빌드와 함께 검증한다.

### 단계 2 — 다운로드 단계만 수정

수정 대상은 `.github/workflows/build-edge-deb.yml`의 `Download MediaMTX ARM64` 단계로 한정한다.

- 단계의 `env`에 압축 파일용 `EXPECTED_ARCHIVE_SHA256` 고정값을 둔다. 파일명과 버전은 현재 값을 유지한다.
- 고정값이 64자리 16진수인지 검사하고, 잘못되었다면 명확한 메시지와 함께 실패한다.
- 존재하지 않는 `checksums.sha256` 다운로드와 해당 파일을 읽는 `awk` 구문을 제거한다.
- 기존 `printf ... | sha256sum --check -` 검증은 고정 기대값을 사용하도록 연결한다.
- 해시 검증이 성공한 뒤에만 `tar`, `chmod`, `vendor/mediamtx --version`을 실행한다.
- 추출 바이너리 해시를 계산하는 기존 코드와 `$GITHUB_ENV` 전달은 유지한다.
- 릴리스 체크섬 파일을 사용한다고 설명하는 기존 주석을 고정된 압축 파일 해시 검증에 맞게 고친다.

구현 결과: `Download MediaMTX ARM64` 단계에 검증된 64자리 SHA-256을 설정하고, 누락·잘못된 문자를 검사한다. 기대값과 다르면 `sha256sum --check`가 압축 해제 전에 중단한다. 없는 체크섬 URL과 체크섬 파싱을 제거했다.

완료 기준: 존재하지 않는 URL과 임시 체크섬 파일 참조가 사라지고, `MEDIAMTX_SHA256`에는 추출 바이너리의 해시가 전달된다. `build_deb.sh`와 `verify_deb.sh`는 변경할 필요가 없다. 달성.

### 단계 3 — 정상·실패 경로 검증

실제 자산의 해시 일치와 변조 파일 거부는 로컬에서 검증한다. ARM64 실행과 전체 DEB 빌드·artifact 검증은 수정 커밋을 저장소에 푸시한 다음 Actions에서 마무리한다.

| 검사 | 수행 방법 | 통과 기준 |
|---|---|---|
| YAML·셸 구문 | 다운로드 단계 추출 후 Bash 문법 검사 | Git Bash의 `bash -n` 통과 |
| 정상 다운로드 | 고정 해시와 일치하는 테스트 압축 파일 사용 | 해시 통과, 실행 파일 검사와 `MEDIAMTX_SHA256` 생성 통과 |
| 해시 불일치 | 다른 정상 형식의 기대 해시 사용 | Actions와 같은 Bash `-e -o pipefail` 옵션에서 tar·바이너리 실행 전에 실패 |
| 파일 변조 | 다운로드 파일의 바이트를 바꾼 뒤 검증 | 압축 해제·바이너리 실행 전에 비정상 종료 |
| 설정 오류 | 기대 해시 누락 또는 잘못된 형식 입력 | 명확한 오류를 출력하고 중단 |
| 다운로드 오류 | 존재하지 않는 자산 URL로 별도 실행 | curl 실패 후 후속 실행 중단 |

실패 경로에서 단순히 최종 명령이 실패했는지만 보지 않고, 압축 해제와 바이너리 실행이 시작되지 않았는지도 확인한다.

로컬 검증 완료: 공식 ARM64 자산의 실제 SHA-256이 고정값과 일치했다. 다운로드 단계 스크립트의 Bash 문법 검사에 통과했고, 임시 디렉터리에서 정상 경로는 해시를 검사한 뒤 실행 파일 해시를 전달했으며 해시 불일치 경로는 Actions와 같은 `-e -o pipefail` 옵션에서 tar 호출 전에 중단됐다. ARM64 바이너리 실행 및 전체 DEB 빌드는 Actions에서 확인한다.

### 단계 4 — 수정 커밋으로 Actions 전체 빌드 확인 (미완료)

1. 수정 내용을 포함한 브랜치에서 `Build Edge DEB`를 실행한다. 기존 실패 실행을 그대로 재실행하는 것만으로는 수정 커밋이 적용되지 않을 수 있으므로 실행 대상 SHA를 확인한다.
2. `ubuntu-24.04-arm`에서 다운로드·해시 검증·버전 확인·DEB 빌드가 모두 성공하는지 확인한다.
3. 빌더가 호출하는 `verify_deb.sh`와 `sha256sum -c *.deb.sha256`의 성공을 확인한다.
4. `ai-cctv-edge-arm64` artifact에서 `.deb`와 `.deb.sha256`을 내려받아 존재 여부와 체크섬을 확인한다. `dpkg-deb -f`로 패키지 이름·소스 버전·`arm64` 아키텍처를 확인한다.
5. 실행 URL, 실행 커밋 SHA, artifact 이름과 검증 결과를 수정 기록에 남긴다.

완료 기준: 수정 커밋의 Actions 실행이 성공하고, 생성된 ARM64 DEB와 체크섬을 실제로 내려받아 검증했다.

## 5. 변경 범위와 남는 확인 사항

필수 코드 변경은 워크플로 한 파일이다. MediaMTX 버전 업그레이드, Actions 버전 변경, Python 의존성 변경, 패키지 빌더 리팩터링은 이번 원인 해결 범위에 포함하지 않는다. 기존 `edge/README.md` 미커밋 변경도 보존한다.

현재 확인한 것은 다운로드 단계의 원인이다. 이 문제를 고친 뒤 Python wheel 수집이나 DEB 검사에서 별도 오류가 드러날 수 있으므로, 다운로드 단계 성공만으로 전체 빌드 복구를 선언하지 않는다.

Raspberry Pi OS Bookworm ARM64에서 설치·서비스 실행·실제 송출을 확인하는 것은 장치 배포의 후속 인수시험이다. CI 빌드 성공과 실장비 운영 성공은 각각 결과를 기록한다.

## 6. 참고 자료

- [MediaMTX v1.9.0 공식 릴리스](https://github.com/bluenviron/mediamtx/releases/tag/v1.9.0)
- [공식 릴리스 API: 자산 목록·크기·digest](https://api.github.com/repos/bluenviron/mediamtx/releases/tags/v1.9.0)
- [v1.9.0 당시 공식 릴리스 워크플로](https://github.com/bluenviron/mediamtx/blob/v1.9.0/.github/workflows/release.yml)
- [수정 대상 워크플로](../.github/workflows/build-edge-deb.yml)
- [DEB 빌더](../edge/packaging/build_deb.sh)
- [DEB 검증 스크립트](../edge/packaging/verify_deb.sh)
