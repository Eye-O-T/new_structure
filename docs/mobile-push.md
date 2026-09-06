# Android 통합과 FCM 푸시

2026-09-06 통합. Android 우선, 기존 Firebase 프로젝트 사용, **모든 이벤트 알림**을 초기값으로 한다.
앱에서 필요하면 사람 감지·장애·위험 상태만 받도록 변경할 수 있다.

## 구조와 책임

```text
Edge / Preprocessing / External 상태 수집
  → Data.create_event (기존 중앙 이벤트)
      → 같은 SQLite 트랜잭션으로 push_deliveries 생성
          → External 발송 워커가 내부 API에서 1건 lease
              → Firebase Cloud Messaging → Android
                  → 알림 클릭 → 중앙 로그인/ACL 확인 → 이벤트 상세 → 연결 녹화
```

- SQLite를 여는 서비스는 Data뿐이다. 새 SQL migration `004_mobile_push.sql`은 기존 DB에 추가 적용된다.
- 모든 이벤트 생산자가 기존 `create_event`를 사용하므로 추론·장애·복구·프로필 이벤트를 동일하게 처리한다.
- `mobile_devices`는 사용자, 로그인 refresh family, 역할, 기기 토큰, 수신 설정을 보관한다.
- 이벤트 생성 시 수신자를 확정한다. 새로 로그인한 기기에 과거 이벤트를 소급 발송하지 않는다.
- 생성 시와 발송 claim 시 활성 사용자·역할·refresh family·Camera ACL·수신 설정을 검사한다.
- 로그아웃의 refresh 철회는 해당 family 기기 등록과 대기열을 삭제한다. 토큰 회전은 등록을 유지한다.
- FCM 토큰은 한 계정/기기 등록에만 연결한다. 재등록으로 소유자·토큰이 바뀌면 이전 대기열을 취소한다.
- 모바일은 로그인마다 새 device ID를 만들며 같은 로그인 세션을 복원·갱신할 때만 유지한다.
  이전 계정·로그인 세션의 지연 알림으로 새 세션의 이벤트 화면을 열지 않는다.

## 발송 의미와 장애 처리

- 기본 poll 5초, 한 번에 1건 claim, lease 2분. 프로세스가 중단되면 lease 만료 뒤 재시도한다.
- 실패는 30초부터 지수 backoff, 최대 15분 간격, 최대 8회다. 생성 후 1시간이 지나면 취소한다.
- 완료·실패·취소된 대기열 기록은 다음 claim 정리 과정에서 7일 후 제거한다.
- `UnregisteredError`는 해당 토큰 등록을 제거한다. 전송 오류 원문이나 토큰은 로그에 남기지 않는다.
- 이벤트 저장은 FCM 가용성에 의존하지 않는다. 네트워크가 끊겨도 이벤트와 대기열은 저장된다.
- 전송 성공 직후 프로세스가 죽으면 재전송될 수 있다. 정확히 한 번 배달을 보장하지 않는다.
  Android 알림 tag와 포그라운드 event ID 중복 억제를 사용한다.
- FCM 성공은 FCM 접수 성공이다. 기기 도착/사용자 확인을 보장하지 않는다.
- 이미 FCM에 접수된 알림은 로그아웃 후 취소할 수 없다. 잠금 화면에는 일반 안내만 표시하고,
  상세 조회는 로그인 세션과 중앙 ACL을 다시 검증한다.
- 복구한 과거 Edge 이벤트도 중앙에 새로 저장되면 알림 대상이다. 원래 발생 시각을 상세에 표시한다.

## 중앙 서버 설정

기본 중앙 배포는 FCM 없이 계속 동작한다. Firebase 키가 없으면 이 단계는 보류한다.

1. 기존 Firebase 프로젝트에서 Cloud Messaging API를 활성화한다.
2. 해당 프로젝트에 FCM 발송 권한이 있는 서비스 계정 JSON을 준비한다.
   파일은 소스 저장소 밖의 제한된 경로에 보관하고 External 컨테이너 사용자가 읽을 수 있게 한다.
3. Configurator가 만든 `.env` 또는 `compose.env`와 **같은 폴더**에 `push.env`를 만든다.
   [예시](../server/push.env.example)를 복사하여 다음을 지정한다.

```dotenv
PUSH_ENABLED=true
FIREBASE_PROJECT_ID=your-existing-project-id
FIREBASE_SERVICE_ACCOUNT_FILE='C:/ProgramData/AI_CCTV/secrets/firebase-service-account.json'
```

4. Configurator의 **Start services**를 실행한다. 기존 컨테이너가 있어도 `up --build`로 설정을 적용한다.
   단순 Restart는 컨테이너 환경을 재생성하지 않으므로 최초 설정 변경 적용에는 Start를 사용한다.

ComposeAdapter는 활성화된 `push.env`가 있을 때 두 번째 env 파일과 `compose.push.yml`을 추가한다.
서버 설정 재생성은 별도 `push.env`를 덮어쓰지 않는다. 중지·상태·로그 명령도 같은 구성을 사용한다.
수동 Compose를 사용할 경우 기존 배포 env를 먼저, push env를 다음에 지정한다.

```powershell
docker compose --env-file C:/path/to/compose.env --env-file C:/path/to/push.env `
  -f server/compose.yml -f server/compose.push.yml up -d --build --wait
```

운영자 없이 앱에서 관리자 키를 받거나 Firebase 프로젝트를 생성하지 않는다.
Firebase Admin 서비스 계정은 APK에 넣지 않는다. 영상·스냅샷·JWT·비밀번호는 FCM payload에 넣지 않는다.

## Android 설정

1. 기존 Firebase Android 앱의 `google-services.json`을 `mobile/android/app/google-services.json`에 둔다.
2. 앱 식별자는 기존 `com.example.app`을 유지했다. JSON의 등록 package name과 반드시 일치해야 한다.
   다른 package name으로 이전하려면 Firebase 앱 등록과 Android applicationId를 함께 변경한다.
3. [모바일 README](../mobile/README.md)에 따라 실행한다. 앱에서 중앙 HTTPS origin으로 로그인한다.
4. Android 알림 권한을 허용하고 설정 화면의 알림 연결 상태를 확인한다.

키 파일이 없는 checkout에서도 Android debug 빌드는 Google Services 플러그인을 조건부로 생략한다.
Firebase 초기화 실패는 일반 로그인·조회 화면을 막지 않는다. iOS 및 다른 플랫폼은 이번 인수 대상이 아니다.

## 공개 API

모든 경로는 `/api/v1/notifications` 아래이며 중앙 Access Bearer 또는 Access Cookie 인증이 필요하다.

| 메서드·경로 | 용도 |
| --- | --- |
| `GET /status` | 서버의 푸시 활성 설정 확인 (`enabled`, `provider`), 기기 배달 확인은 아님 |
| `PUT /devices` | 현재 사용자·유효한 refresh 세션에 기기 등록·수신 설정 갱신 |
| `DELETE /devices/{device_id}` | 현재 사용자 소유 등록만 제거; 반복 요청 가능 |

등록 Body:

```json
{
  "device_id": "0123456789abcdef0123456789abcdef",
  "token": "<native-fcm-token>",
  "refresh_token": "<current-rotating-refresh-token>",
  "platform": "android",
  "enabled": true,
  "event_types": null
}
```

`event_types=null`은 모든 유형, `[]`는 수신 유형 없음이다. 목록을 보내면 해당 문자열 유형만 받는다.
사용자 ID나 역할은 클라이언트가 정하지 않는다. 등록 응답은 token·refresh token을 반환하지 않는다.
API 401이면 단일 refresh 작업으로 갱신한 뒤 한 번 재시도한다. 등록 Body의 refresh token도 최신값으로 바꾼다.
로그아웃에는 현재 refresh token을 반드시 보낸다. 서버 연결 실패 시 앱은 로그아웃 완료로 표시하지 않고 재시도를 안내한다.

내부 `/mobile-devices`, `/push-deliveries/*`는 External 내부 서비스 토큰만 사용할 수 있다.
공개 listener에는 내부 API를 노출하지 않는다. 공개 테스트 발송 API는 추가하지 않았다.

## 실제 인수 시험

자동 테스트는 외부 발송을 mock하며 실제 기기에 메시지를 보내지 않는다. 설정 완료 후 다음을 확인한다.

1. 실제 카메라에서 중앙 이벤트를 발생시켜 DB 저장 → 기기 알림 → 상세 → 연결 녹화를 확인한다.
2. 앱 포그라운드·백그라운드·종료 상태 각각에서 수신과 알림 클릭을 확인한다.
3. 알림 거부·설정 해제, 앱 복귀, access 만료·refresh 회전을 확인한다.
4. Viewer의 허용되지 않은 Camera는 발송되지 않는지, ACL 회수·비활성화·로그아웃 뒤 대기 메시지가 취소되는지 확인한다.
5. 같은 기기 재로그인 후 이전 세션 알림을 눌러도 새 계정 화면이 열리지 않는지 확인한다.
6. 인터넷 단절과 External 재시작 뒤 대기열 재시도, Android 토큰 교체, 앱 삭제 후 무효 토큰 제거를 확인한다.
7. LTE/외부 Wi-Fi에서 HTTPS 인증서, 모든 HLS 요청의 인증, 15분 이상 재생과 재연결을 확인한다.

Android 강제 중지, OS 전력 정책, FCM 지연은 별도 실기기 검증 대상이다. 기록의 정본은 중앙 이벤트 API다.
