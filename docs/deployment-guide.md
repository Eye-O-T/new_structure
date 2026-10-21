# AI CCTV 배포 안내

졸업작품 시연 구성은 Windows Server와 Raspberry Pi Edge를 신뢰 LAN에 연결합니다. 목표는
2대 동시 시청이며 최대 4대 이하를 지원 범위로 검증합니다. 서버의 공개 주소와 카메라
주소는 LAN에서 실제로 도달 가능한 주소를 사용합니다.

## Windows Server 설치

Windows 10/11 x64, Docker Desktop Linux container mode, Docker Compose v2, Python 3.11,
Git, `uv`가 필요합니다. 저장소 루트에서 다음 명령으로 Installer를 실행합니다.

```powershell
uv sync --project server/setup/install_helper --locked
uv run --project server/setup/install_helper --locked python -m server.installer
```

설치 마법사에서 데이터 위치, 공개 주소, 관리자 비밀번호, 네트워크 bind 주소를 확인하고
TLS 설정을 선택합니다. 운영 Compose 설정과 비밀 파일은 ProgramData 아래에 생성됩니다.
설치 후 Server Manager에서 서버 상태를 확인하고, 실제 PC에서 사용할 LAN 주소로 관리
화면에 접속합니다. 소스 실행, CLI, 설치 파일 빌드는 [호스트 도구 안내](../server/README.md)
및 [설치 도우미 안내](../server/setup/install_helper/README.md)를 참조하십시오.

### 소스 배포

개발용 설치는 저장소 루트에서 위 `uv sync` 및 Installer 실행 명령을 사용합니다. 공식
Windows 설치 파일을 빌드할 때는 Python 3.11, `uv`, Inno Setup 6가 필요하며
[설치 도우미 안내](../server/setup/install_helper/README.md#설치-파일-빌드)의 절차를
따릅니다. 운영 Compose 설정은 ProgramData 아래에 두고 소스 설정과 섞지 마십시오.

AI 감지·인물 연결은 기본 영상 수신·녹화와 분리되어 있습니다. 기능을 켜려면 호환되는
YOLO 탐지 모델과 OSNet ONNX 모델을 준비하고 설치 설정에서 모델을 지정합니다. 모델 파일
존재 확인은 실제 모델의 정확도나 장비 처리량을 보장하지 않습니다. 자세한 준비는
[모델 도구 안내](../server/tools/README.md)와 서비스별 README를 따릅니다.

## Edge 배포

Pi는 Raspberry Pi OS Bookworm ARM64와 프로젝트의 Edge DEB를 사용합니다. 최초 pairing,
수동 설정, 체크섬 검증은 [Edge 안내](../edge/README.md)에 있습니다. 장치 발견과 최초
설정은 신뢰 LAN 안에서 수행하십시오.

## 모바일과 푸시

모바일 APK는 `mobile/`에서 다음과 같이 빌드합니다. Firebase 푸시를 시연할 때는 동일
프로젝트의 Android Firebase 설정과 Server 서비스 계정 설정도 필요합니다.

```sh
cd mobile
flutter pub get
flutter analyze
flutter test
flutter build apk --release --dart-define=API_BASE_URL=https://cctv.example.com
```

HTTP는 신뢰 LAN에서만 선택하고, HTTPS는 단말이 신뢰하는 인증서를 사용합니다. 설정과
푸시 항목은 [Android 안내](../mobile/README.md)를 참고하십시오.

Server에서 `PUSH_ENABLED=true`와 Firebase 서비스 계정 파일을 준비하고, Android 앱에는
같은 Firebase 프로젝트의 설정을 포함합니다. 실제 단말에서 권한과 알림 수신을 확인한 뒤
시연하십시오. 비밀키 파일은 저장소에 추가하지 않습니다.

## 네트워크 범위

같은 LAN에서 Edge→Server RTSP/TCP 8554, Host→Edge 관리 TCP 8003 및 복구 TCP 8002,
Edge 발견 UDP 37020을 사용합니다. 공개 HTTP(S)와 RTSP의 bind 주소를 실제 LAN 주소에
맞추고 필요한 방화벽 규칙만 허용합니다. 관리·복구 API와 RTSP를 인터넷에 직접 공개하지
않습니다. 서비스 간 상세 흐름은 [구성·네트워크 문서](architecture.md)를 따릅니다.
