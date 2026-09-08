# AI CCTV Android

중앙 서버의 HTTPS API를 사용하는 Flutter 앱이다. 원본은 `Eye-O-T/AI_CCTV-Mobile`의
`401d5424b501efdf09dd0ab2545487eb99858760`이며 현재 서버 계약에 맞게 연동했다.

## 포함 기능

- HTTPS 로그인·세션 저장·토큰 갱신·로그아웃
- 카메라 목록, 보호된 Live HLS, 장치 상태, 관리자 HD/FHD 변경
- 실시간 사람 박스·카메라 내 인물 ID·연결된 전역 ID 표시 ([시차와 좌표 계약](../docs/openapi.yaml))
- 로컬 날짜별 이벤트 검색, UTC 쿼리, 페이지 처리, 이벤트 상세·연결 녹화 재생
- FCM 기기·권한 관리, 모든 이벤트 기본 수신, 알림 클릭과 앱 복귀 시 재조회

카메라 최초 등록·Edge Pairing은 Configurator가 담당한다. 독립 녹화 검색·사용자/ACL 편집 화면은
없다. iOS·Web·데스크톱은 기본 템플릿만 있으며 제품 동작은 검증하지 않았다.

## 개발 실행

검증 도구는 Flutter 3.47.2 / Dart 3.13.2다. `pubspec.lock`을 함께 사용한다.
Android SDK와 JDK는 Android Studio 등으로 준비하고 `flutter doctor`에서 확인한다.

```powershell
cd mobile
flutter pub get
flutter analyze
flutter test
flutter run --dart-define=API_BASE_URL=https://cctv.example.com
```

`API_BASE_URL`은 로그인 화면 기본값이며, 생략하면 화면에서 주소를 입력한다.
서버 origin만 입력하고 `/api/v1`을 붙이지 않는다. HTTP와 다른 origin의 미디어 URL은 거부한다.
인증서 검증을 끄는 개발 우회는 제공하지 않는다. 실제 기기가 신뢰하는 인증서를 준비한다.

Windows에서 사용하지 않는 데스크톱 템플릿 때문에 symlink 권한 오류가 나면,
아래처럼 **현재 명령 세션에서만** desktop 생성을 끄고 다시 실행할 수 있다.

```powershell
$env:FLUTTER_WINDOWS='false'
$env:FLUTTER_LINUX='false'
flutter pub get
```

Flutter SDK 경로에 공백이 있으면 일부 native build hook 도구가 실패할 수 있다.
공백 없는 SDK 경로 또는 Windows short path로 실행한다. 테스트 실행을 위해 플랫폼 폴더를 삭제할 필요는 없다.

## Firebase와 배포

[중앙 FCM 설정 및 인수 시험](../README.md#모바일과-푸시)을 따른다.
`android/app/google-services.json`이 없으면 Firebase 연결만 비활성 상태로 시작한다.
실제 푸시를 받으려면 기존 프로젝트의 Android 설정과 중앙 서비스 계정이 모두 필요하다.
앱은 최초 로그인 때 Android 알림 권한을 요청한다.

현재 Android `applicationId`는 `com.example.app`이다. 기존 Firebase의 등록 package name과
일치하는지는 설정 파일을 받은 뒤 확인해야 한다. 릴리스 서명을 위해 `android/key.properties`를 만든다.

```properties
storeFile=C:/secure/path/release.jks
storePassword=<local-password>
keyAlias=<local-alias>
keyPassword=<local-password>
```

```powershell
flutter build apk --release --dart-define=API_BASE_URL=https://cctv.example.com
```

서명 설정이 없는 release 빌드는 명시적으로 중단한다. debug 키를 release에 사용하지 않는다.
Firebase 파일·서명 파일·세션·키는 Git에 포함하지 않는다.

## 검증 경계

로그인·토큰 회전·이벤트 계약·URL 검증·알림 식별·화면 이동은 Flutter 자동 테스트로 검증한다.
실제 Android APK 빌드, HLS/복구 MPEG-TS의 기기별 재생, FCM 실제 도착은 Android SDK·실기기와
Firebase 설정을 준비한 후 검증해야 한다. 자동 테스트 통과를 기기 수신 성공으로 간주하지 않는다.
