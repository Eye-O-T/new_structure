# External 서비스

모바일·Configurator가 사용하는 API다. 사용자 인증, 카메라 접근 권한, Edge 상태 수집과 Firebase Cloud Messaging(FCM) 푸시 발송을 담당한다. SQLite에 직접 접근하지 않고 Data 내부 API를 호출한다.

## 코드 구성

| 경로 | 역할 |
|---|---|
| `app/main.py` | 서버·백그라운드 작업 시작과 종료 |
| `app/api/` | 인증·사용자·카메라·이벤트·녹화·객체·알림·영상 인증 API |
| `app/security/` | 비밀번호·토큰·사용자·카메라 권한 검사 |
| `app/clients/` | Data·Edge·MediaMTX 통신과 응답 오류 변환 |
| `app/notifications/firebase.py` | Firebase 발송 연결부 |
| `app/workers/` | Edge 상태·이벤트 수집, Data 푸시 대기열 발송 |
| `app/camera_lifecycle.py` | 카메라 변경과 영상 인증이 동시에 실행될 때의 순서 제어 |
| `app/schemas.py` | 공개 API 요청·응답 모델 |
| `tests/` | API·인증·영상 권한·동시 실행 검증 |

모바일을 새로 구현할 때는 [OpenAPI](../../../docs/openapi.yaml)를 기준으로 한다. 실행 중인 서버에서는 공개 HTTPS 주소 뒤에 `/api/v1/docs`를 붙이면 요청·응답을 확인할 수 있다.

## 실행과 검증

[개발 환경](../../../README.md#서버-코드-개발)을 준비하고 개발 Compose를 기동한 뒤, 저장소 루트에서 실행한다. 아래 `server/.env`는 개발 전용 설정이다.

```sh
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml exec external python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/external/tests -q
```

기본 비밀 설정 파일은 `server/secrets/external.env`다. `/health/live`는 프로세스 상태, `/health/ready`는 Data 통신 상태다. 이 내부 점검 주소가 아닌 공개 `/api/v1/system/status`는 관리자 인증 후 조회한다.

## 유지해야 하는 경계

- Nginx·MediaMTX는 `app/api/media_auth.py`를 통해 영상 접근 권한을 확인한다. 카메라 변경·인증이 동시에 실행되어도 비활성 카메라가 허용되지 않도록 기존 순서 제어를 유지한다.
- 이벤트·푸시 대기열·재시도 상태는 Data가 저장한다. External은 대기 작업을 받아 발송하고 결과를 보고한다.
- FCM 발송은 별도 설정이 필요하다. 서버 기동만으로 푸시가 활성화되지는 않는다. 설정과 실제 단말 확인은 [모바일과 푸시](../../../README.md#모바일과-푸시)를 따른다.

전체 통신 흐름은 [구조 문서](../../../docs/architecture.md), 서비스 간 검증은 [서버 자동 테스트](../../../README.md#서버-자동-테스트)를 따른다.
