# AI CCTV Android

중앙 서버의 HTTPS API를 사용하는 Flutter 앱이다. 원본은 `Eye-O-T/AI_CCTV-Mobile`의
`401d5424b501efdf09dd0ab2545487eb99858760`이며 현재 서버 계약에 맞게 연동했다.

## 포함 기능

- HTTPS 로그인·세션 저장·토큰 갱신·로그아웃
- 카메라 목록, 보호된 Live HLS, 장치 상태, 관리자 HD/FHD 변경
- 실시간 사람 박스·카메라 내 인물 ID(`P`)·연결된 전역 ID(`G`) 표시
- 로컬 날짜별 이벤트 검색, UTC 쿼리, 페이지 처리, 이벤트 상세·연결 녹화 재생
- FCM 기기·권한 관리, 모든 이벤트 기본 수신, 알림 클릭과 앱 복귀 시 재조회

사람 박스와 `P`는 중앙 객체 처리 파이프라인이 활성화되어 최신 좌표를 제공할 때 표시된다. 전역 인물 연결은 현재 미구현이므로 기본 구성에서는 `G`가 표시되지 않는다. 박스는 최신 좌표를 별도로 조회해 그리며 영상 지연 때문에 사람 위치와 어긋날 수 있다. 좌표 규약은 [OpenAPI](../docs/openapi.yaml)를 참고한다.

최초 Edge 검색·연결은 [설치 도우미](../server/setup/install_helper/README.md), 운영 중 Edge·카메라 등록과 관리는 [서버 관리자 화면](../docs/guide.md#서버-관리자-화면)을 사용한다. 앱에는 독립 녹화 검색·사용자 및 카메라 접근 권한 편집 화면이 없다. iOS·Web·데스크톱 폴더는 남아 있지만 제품 동작은 검증하지 않았다.

## 앱 설치와 로그인

배포 담당자에게 받은 서명된 APK를 Android 기기에서 열어 설치한다. 기기가 요청하면 해당 파일을 연 앱의 설치 권한을 허용한다. 개발 도구는 필요하지 않다.

로그인 화면에는 중앙 서버의 HTTPS 주소와 서버에서 만든 계정을 입력한다. 앱 자체 회원가입은 없다. 주소는 `https://cctv.example.com`처럼 도메인과 필요 시 포트까지만 입력하며 `/api/v1`은 붙이지 않는다. 기기에서 접근 가능한 주소와 신뢰하는 인증서가 필요하다. `localhost`는 중앙 PC가 아닌 휴대전화 자신을 가리킨다.

## 개발 실행

검증 도구는 Flutter 3.47.2 / Dart 3.13.2다. `pubspec.lock`을 함께 사용한다. 현재 lockfile이 요구하는 최소 버전은 Flutter 3.44.0 / Dart 3.12.2이며, SDK 설치 후 `flutter --version`으로 확인한다.
Android SDK와 Java 17을 지원하는 JDK를 준비하고 `flutter doctor -v`에서 확인한다. Android 라이선스 동의가 필요하다고 나오면 `flutter doctor --android-licenses`를 실행한다. 아래 명령은 저장소 루트에서 시작하며, 이후 작업 위치는 `mobile/`이다. Android 기기의 USB 디버깅을 켜고 PC 연결을 허용하거나 Android 에뮬레이터를 실행한다.

```sh
cd mobile
flutter pub get
flutter analyze
flutter test
flutter devices
flutter run -d '<Android-기기-ID>' --dart-define=API_BASE_URL=https://cctv.example.com
```

`<Android-기기-ID>`는 `flutter devices`에 표시된 값으로 바꾼다. `API_BASE_URL`은 로그인 화면 기본값이며 생략해도 화면에서 입력할 수 있다. HTTP와 로그인 서버의 호스트·포트가 다른 영상 URL은 거부하며 인증서 검증을 끄는 우회는 제공하지 않는다.

Windows에서 사용하지 않는 데스크톱 템플릿 때문에 symlink 권한 오류가 나면,
아래처럼 **현재 명령 세션에서만** desktop 생성을 끄고 다시 실행할 수 있다.

```powershell
$env:FLUTTER_WINDOWS='false'
$env:FLUTTER_LINUX='false'
flutter pub get
```

Flutter SDK 경로의 공백으로 빌드 도구가 실패하면 공백 없는 SDK 경로를 사용한다.

## Firebase와 배포

[중앙 FCM 설정 및 인수 시험](../docs/guide.md#모바일과-푸시)을 따른다.
`android/app/google-services.json`이 없으면 Firebase 연결만 비활성 상태로 시작한다.
실제 푸시를 받으려면 같은 Firebase 프로젝트의 Android 설정과 중앙 서비스 계정이 모두 필요하다. `google-services.json`은 빌드 전에 배치하며, 파일을 추가하거나 바꾼 뒤에는 APK를 다시 빌드·설치한다.
Firebase가 설정된 앱은 로그인 후 Android 알림 권한을 요청한다. 앱의 **설정 → 알림 연결 상태**에서 등록 결과를 확인하고 필요하면 다시 시도한다. **모든 이벤트 알림**을 끄면 사람 감지·장애·위험 상태만 수신하며, **푸시 알림**을 끄면 해당 기기의 등록을 해제한다.

현재 Android 앱 식별자(`applicationId`)는 `com.example.app`이다. Firebase에 등록한 Android 패키지 이름과 같아야 한다. 릴리스 빌드는 배포용 서명 키(`.jks`)가 필요하다. `mobile/`에서 `android/key.properties`를 만들고 실제 키 경로·비밀번호를 입력한다.

각 항목을 `키=값` 형식으로 한 줄씩 저장한다. Windows 키 경로에는 아래처럼 `/`를 사용한다. 예시 자리표시자를 실제 값으로 바꾸며 값을 따옴표로 감싸지 않는다.

```properties
storeFile=C:/secure/path/release.jks
storePassword=<local-password>
keyAlias=<local-alias>
keyPassword=<local-password>
```

설정 후 `mobile/`에서 실행한다.

```sh
flutter build apk --release --dart-define=API_BASE_URL=https://cctv.example.com
```

결과는 `mobile/build/app/outputs/flutter-apk/app-release.apk`다. 같은 앱의 업데이트는 기존 서명 키를 유지한다. 서명 설정이 없는 release 빌드는 중단하며 Firebase 파일·서명 파일·세션·키는 Git에 포함하지 않는다.

## 검증 경계

로그인·토큰 회전·이벤트 계약·URL 검증·알림 식별·화면 이동은 Flutter 자동 테스트로 검증한다.
실제 Android APK 빌드, HLS/복구 MPEG-TS의 기기별 재생, FCM 실제 도착은 Android SDK·실기기와
Firebase 설정을 준비한 후 검증해야 한다. 자동 테스트 통과를 기기 수신 성공으로 간주하지 않는다.
