# External 서비스

모바일·Configurator의 공개 API, 사용자 인증, 카메라 접근 권한, Edge 상태 수집과 FCM 발송을 담당합니다. SQLite에 직접 접근하지 않고 `clients/data.py`를 통해 Data 내부 API를 호출합니다.

## 코드 구성

| 경로 | 역할 |
|---|---|
| `app/main.py` | FastAPI 생성, 라우터·오류 처리 등록, 백그라운드 작업 시작·종료 |
| `app/api/` | 인증·사용자·카메라·이벤트·녹화·객체·알림·영상 인증 API |
| `app/api/validation.py` | 리소스 ID·시간 범위 등 공통 요청 검증 |
| `app/api/representations.py` | 비밀번호 등 내부 필드를 제외한 공개 사용자 응답 |
| `app/security/` | Argon2 비밀번호, JWT, 로그인 실패 지연, 사용자·카메라 권한 검사 |
| `app/clients/` | Data·Edge·MediaMTX 통신과 응답 오류 변환 |
| `app/notifications/firebase.py` | Firebase SDK에만 의존하는 발송 어댑터 |
| `app/workers/` | Edge 상태·이벤트 수집, Data 푸시 대기열 발송 |
| `app/camera_lifecycle.py` | 카메라 등록·비활성화·인증키 교체·미디어 게시 인증의 동시 실행 제어 |
| `app/dependencies.py` | 요청에 설정·클라이언트·공유 잠금 제공 |
| `app/errors.py` | 내부 오류·검증 입력의 민감 정보가 공개 응답에 유출되지 않도록 변환 |
| `app/schemas.py` | 공개 API 요청·응답 모델 |
| `tests/` | 서비스 동작·인증·영상 권한·동시 실행 검증 |

`api/recordings.py`는 관리자 녹화 복구 작업 조회도 제공합니다. 시스템 상태 조회는 `api/health.py`에 있습니다. 앱 공개 계약은 `/api/v1/openapi.json`과 `/api/v1/docs`에서 확인합니다.

## 실행과 검증

저장소 루트에서 전체 중앙 서비스와 함께 실행합니다.

```sh
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml up -d --build
docker compose --env-file server/.env -f server/compose.yml -f server/compose.dev.yml exec external python -m pytest -c tests/runner/pytest.ini --rootdir=. server/services/external/tests -q
```

운영과 분리한 개발용 설정·인증키를 사용합니다. `/health/live`는 프로세스 상태, `/health/ready`는 Data 통신 상태를 확인합니다. 서비스 간 인증키·JWT·영상 자격 증명 설정은 `app/config.py`와 서버 설정 예시를 기준으로 합니다.

## 유지해야 하는 경계

- Nginx의 영상 권한 확인과 MediaMTX의 게시·읽기 인증은 `api/media_auth.py`가 처리합니다. 허용 카메라·URI 검증을 우회하는 별도 공개 경로를 추가하지 않습니다.
- 카메라 비활성화·삭제·게시 인증키 교체와 MediaMTX 인증은 같은 제한 크기 잠금 풀을 사용합니다. 카메라 제어 경로를 변경할 때 이 순서를 유지합니다.
- 로그인은 보안 쿠키와 모바일용 토큰 응답을 지원합니다. 토큰 갱신은 Data의 회전 세션을 사용하고, 로그아웃은 관련 토큰·기기 수신을 해제합니다.
- 이벤트 저장·푸시 작업 생성·발송 재시도 상태는 Data가 관리합니다. External은 발송 작업을 임대하고 결과만 반환합니다.
- FCM은 설정 시에만 실행합니다. SDK 오류·기기 토큰·서비스 계정 내용을 로그에 남기지 않습니다.
- 실제 Firebase·Android 단말·영상 전송 검증은 자동 테스트와 별개입니다.

관련 문서: [모바일/API 연동](../../../docs/openapi.yaml), [FCM 설정](../../../README.md#모바일과-푸시), [설계](../../../docs/architecture.md).
