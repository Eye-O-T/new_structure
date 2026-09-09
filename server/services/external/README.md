# External 서비스

모바일·서버 설치 도우미가 사용하는 API와 서버 관리자 화면을 제공한다. 사용자 인증, 카메라 접근 권한, Edge 상태 수집과 Firebase Cloud Messaging(FCM) 푸시 발송을 담당한다. SQLite에 직접 접근하지 않고 Data 내부 API를 호출한다.

## 서버 관리자 화면

서버의 공개 HTTPS 주소 뒤에 `/admin/`을 붙여 접속하고 관리자 계정으로 로그인한다. 카메라 목록, Edge·카메라 상태, 등록 정보 수정, 게시 계정 재발급, 지원 화질 조회·변경, External·Data 상태 조회를 제공한다. 전체 컨테이너 시작·중지와 기동 진단은 서버 PC의 설치 도우미 또는 Docker에서 수행한다.

Edge를 수동으로 추가할 때는 [Edge 수동 연결](../../../edge/README.md#수동-연결)에 따라 장치의 인증 토큰을 내보내고, 화면에서 장치 ID·카메라 ID·관리 주소·복구 주소와 함께 등록한다. 서버가 반환한 게시 계정 JSON을 접근이 제한된 폴더에 저장하고, 해당 Edge의 `--publish-credentials-file` 설정에 전달한다. Raspberry Pi도 같은 파일을 설치된 Edge 실행 설정에 적용하고 서비스를 재시작한다.

재발급은 기존 송출을 끊는다. 새 파일을 Edge에 적용하기 전에는 영상이 다시 연결되지 않는다. 다운로드 재시도는 같은 파일을 저장하며, 저장을 확인하고 닫으면 원문을 다시 조회할 수 없다. 브라우저는 로그인 토큰을 현재 탭의 메모리에만 보관하므로 새로고침 후에는 다시 로그인한다.

재발급 도중 Data·MediaMTX 통신에 실패하면 카메라가 비활성 상태로 남을 수 있다. 통신을 복구한 뒤 재발급을 다시 완료하고 새 게시 계정을 Edge에 적용한다. 이전에 사용하던 카메라가 비활성 상태로 남아 있다면 다시 활성화한다. 파일을 잃어버렸다면 원문 조회 대신 재발급이 필요하다.

## 코드 구성

| 경로 | 역할 |
|---|---|
| `app/main.py` | 서버·백그라운드 작업 시작과 종료 |
| `app/api/` | 인증·사용자·카메라·이벤트·녹화·객체·알림·영상 인증 API |
| `app/web/` | 기존 API를 호출하는 한국어 서버 관리자 화면 |
| `app/security/` | 비밀번호·토큰·사용자·카메라 권한 검사 |
| `app/clients/` | Data·Edge·MediaMTX 통신과 응답 오류 변환 |
| `app/notifications/firebase.py` | Firebase 발송 연결부 |
| `app/workers/` | Edge 상태·이벤트 수집, Data 푸시 대기열 발송 |
| `app/camera_lifecycle.py` | 카메라 변경과 영상 인증이 동시에 실행될 때의 순서 제어 |
| `app/schemas.py` | 공개 API 요청·응답 모델 |
| `tools/bootstrap_admin.py` | 소스 배포에서 첫 관리자 생성; 비밀번호 숨김 입력·External의 해시 기능 사용 |
| `tools/export_openapi.py` | 공개 API 명세 생성과 `docs/openapi.yaml` 일치 검사 |
| `tests/` | API·인증·영상 권한·동시 실행 검증 |

모바일을 새로 구현할 때는 [OpenAPI](../../../docs/openapi.yaml)를 기준으로 한다. 실행 중인 서버에서는 공개 HTTPS 주소 뒤에 `/api/v1/docs`를 붙이면 요청·응답을 확인할 수 있다. 공개 API를 변경하면 [서버 자동 테스트](../../../docs/guide.md#서버-자동-테스트)의 OpenAPI 검증·갱신 절차도 수행한다.

첫 관리자 생성 도구는 저장소 루트에서 `python server/services/external/tools/bootstrap_admin.py --username admin`으로 실행한다. 기본 `server/.env` 또는 `--server-dir` 아래 `.env`를 사용하며 설치 도우미의 `compose.env`를 자동 선택하지 않는다. 기존 계정의 비밀번호를 변경하는 명령은 아니다. 설정 도우미 설치의 초기 관리자 준비와 구분하여 [소스 배포](../../../docs/guide.md#소스-배포)에 사용한다.

## 실행과 검증

[개발 환경](../../../docs/guide.md#서버-코드-개발)을 준비하고 개발 Compose를 기동한 뒤, 저장소 루트에서 실행한다. 아래 `server/.env`는 개발 전용 설정이다.

```powershell
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml exec external python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/external/tests -q
```

기본 비밀 설정 파일은 `server/secrets/external.env`다. `/health/live`는 프로세스 상태, `/health/ready`는 Data 통신 상태다. 공개 `/api/v1/system/status`는 관리자 인증 후 조회하며 External·Data 상태를 반환한다. Edge·카메라 상태는 `/api/v1/cameras/{camera_id}/status`에서 별도로 조회한다. 준비 검사 성공은 실제 영상 수신이나 FCM 발송 성공까지 보장하지 않는다.

## 유지해야 하는 경계

- Nginx·MediaMTX는 `app/api/media_auth.py`를 통해 영상 접근 권한을 확인한다. 카메라 변경·인증이 동시에 실행되어도 비활성 카메라의 새 송출·실시간 열람이 허용되지 않도록 기존 순서 제어를 유지한다. 비활성 카메라의 과거 녹화는 사용자 권한에 따라 조회할 수 있다.
- 이벤트·푸시 대기열·재시도 상태는 Data가 저장한다. External은 대기 작업을 받아 발송하고 결과를 보고한다.
- FCM 발송은 별도 설정이 필요하다. 서버 기동만으로 푸시가 활성화되지는 않는다. 설정과 실제 단말 확인은 [모바일과 푸시](../../../docs/guide.md#모바일과-푸시)를 따른다.

전체 통신 흐름은 [구조 문서](../../../docs/architecture.md), 서비스 간 검증은 [서버 자동 테스트](../../../docs/guide.md#서버-자동-테스트)를 따른다.
