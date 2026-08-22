#!/bin/bash


# 라즈베리파이 4 영상 송출 & 10초 로컬 백업 파이프라인 제어 스크립트
# 동작 개요:
#   이 스크립트는 라즈베리파이 libcamera로부터 실시간 영상을 캡처한 뒤,
#   gstreamer의 Y자 분기 tee를 사용하여 스트림을 두 개로 쪼갭니다
#     1) 로컬 백업 전송 : 10초 단위로 영상을 쪼개 TS 파일로 SD카드에 기록
#     2) 실시간 영상 송출 : RTMP 전송망을 타고 로컬 MediaMTX 서버(라즈베리파이에서 돌아감)로 밀어 넣어 RTSP로 변환
#
# 사전 설치 명령어 (라즈베리파이 터미널에서 최초 1회 실행 필요):
#     sudo apt update
#     sudo apt install -y gstreamer1.0-tools gstreamer1.0-plugins-base \
#                        gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
#                        gstreamer1.0-plugins-ugly gstreamer1.0-libav \
#                        gstreamer1.0-rtsp libcamera-v4l2 python3-pip wget tar
# ==============================================================================

# 스크립트 가동 중 에러(오류 코드 반환)가 나면 즉시 스크립트 동작을 멈추고 종료하게 하는 안전 옵션
set -e

# --- 사용자 정의 환경 설정 ---
RESOLUTION_WIDTH=1920     # 전송 및 저장할 비디오의 가로 해상도 (1920픽셀, 1080p)
RESOLUTION_HEIGHT=1080    # 전송 및 저장할 비디오의 세로 해상도 (1080픽셀, 1080p)
FPS=30                    # 초당 프레임 수 (30프레임: 적당한 프레임수)
VIDEO_BITRATE=4000        # H.264 영상 전송률 (kbps, 1080p 권장 시작값)
BACKUP_DIR="./backups"    # 로컬 녹화 세그먼트 파일이 보관될 폴더 경로
RTSP_PATH="live"          # 중계 서버가 송출할 RTSP 서비스 식별 경로 (rtsp://IP:8554/live) 192.168.99.200으로 설정해둠.
MEDIAMTX_VERSION="v1.9.0" # 다운로드할 미디어 프록시 서버(MediaMTX)의 지정 버전

# 녹화본 저장 폴더가 없으면 자동으로 새로 만들어 줍니다. (-p: 부모 폴더도 함께 생성)
mkdir -p "$BACKUP_DIR"

# --- MediaMTX 중계 서버 자동 설치 및 기동 단계 ---
# MediaMTX는 RTMP 스트림을 받아 RTSP 표준 신호로 실시간 중계 대행해 주는 초경량 서버 프로그램
echo "=== Checking RTSP Server (MediaMTX) ==="

# 워크스페이스에 mediamtx이나 mediamtx.yml이 존재하지 않는 경우 다운로드 진행
if [ ! -f "./mediamtx" ] || [ ! -f "./mediamtx.yml" ]; then
    echo "MediaMTX binary or config not found. Detecting architecture and downloading..."
    ARCH=$(uname -m)  # 현재 라즈베리파이 OS의 아키텍처(32비트/64비트)를 실시간 판독
    DOWNLOAD_URL=""
    
    # 64비트 리눅스(AArch64)용 패키지 주소 매칭
    if [ "$ARCH" = "aarch64" ]; then
        echo "Detected 64-bit OS (aarch64)."
        DOWNLOAD_URL="https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_arm64v8.tar.gz"
    # 32비트 리눅스(ARMv7 등)용 패키지 주소 매칭
    elif [[ "$ARCH" =~ "arm" ]]; then
        echo "Detected 32-bit OS (arm)."
        DOWNLOAD_URL="https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_armv7.tar.gz"
    else
        echo "Unsupported architecture: $ARCH. Please download MediaMTX manually."
        exit 1
    fi
    
    # 해당 링크에서 tar.gz 압축 파일을 다운로드하여 실행 파일과 설정 템플릿만 압축 해제
    echo "Downloading MediaMTX from: $DOWNLOAD_URL"
    wget -q --show-progress "$DOWNLOAD_URL" -O mediamtx.tar.gz
    tar -xzf mediamtx.tar.gz mediamtx mediamtx.yml
    rm mediamtx.tar.gz
    echo "MediaMTX binary and config installed successfully."
fi




#미디어mtx가 실행중이지 않을때만 새로 실행하게 하는거임. 안그러면 kill 해야하고 막 귀찮아짐.
# MediaMTX 중계서버가 이미 백그라운드에서 구동 중인지 체크
if pgrep -x "mediamtx" > /dev/null; then
    echo "MediaMTX is already running."
else
    # 실행 중이 아니라면 백그라운드(&)로 실행을 기동하고 모든 로그는 mediamtx.log 파일로 우회 기록
    echo "Starting MediaMTX in background..."
    ./mediamtx > mediamtx.log 2>&1 &
    # 서버 포트(8554, 1935 등)가 정상 리스닝 준비를 마칠 수 있게 2초간 초기화 대기 시간 부여
    sleep 2
fi

# ---  GStreamer 파이프라인 가동 준비 단계 ---
START_TIME=$(date +"%Y%m%d_%H%M%S") # 녹화 파일 식별용 현재 시각 문자열
echo "=== Starting GStreamer Pipeline ==="
echo "Recording files will be saved in: $BACKUP_DIR"
echo "RTSP Stream endpoint: rtsp://localhost:8554/$RTSP_PATH"
echo "Press Ctrl+C to stop streaming and save the current recording segment."

# 종료 정리 헨들러-사용자가 Ctrl+C를 눌렀을 때, 쉘 스크립트 세션이 기동한 백그라운드 중계기까지 깨끗하게 자동 kill하고 퇴근하게 만드는 트랩 설정
#이거 없으면 미디어mtx가 계속 백그라운드에서 돌아가면서 좀비 되고 난리남
cleanup() {
    echo -e "\n=== Shutting down GStreamer and MediaMTX ==="
    # 이 스크립트 실행 환경 하위에서 실행 중인 백그라운드 프로세스 ID(MediaMTX)들을 강제 킬(kill)
    kill $(jobs -p) 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM # Ctrl+C(SIGINT) 및 강제종료(SIGTERM) 신호를 가로채서 cleanup 함수 실행 선언


# [GStreamer 파이프라인 가동]
# -e 플래그: 종료 신호를 받았을 때 GStreamer 내부 녹화 믹서들이 깨지지 않은 완벽한 파일 구조를 디스크에 마감 작성(EOS 전파)하도록 유도하는 핵심 종료 보장 명령어
#
# 설계 배경 (RTMP 우회 송출):
#   GStreamer의 'rtspclientsink' 플러그인은 라즈베리파이의 일부 라이브러리 링크와 핀(Pad) 매칭에 만성 버그가 있어 에러를 자주 유발합니다.
#   이를 완벽히 방해 없이 우회하기 위해, 라즈베리파이 내부의 로컬 루프백 RTMP 포트(1935)로 가볍게 인코딩 송출(`rtmpsink`)하면,
#   상주해 대기하던 MediaMTX 서버가 이 신호를 실시간 가로채어 PC 수신용 RTSP 포트(8554)로 실시간 가교 중계하도록 견고히 설계되었습니다.
#
# 만약 라즈베리파이의 하드웨어 인코더 가속(v4l2h264enc)을 사용하고 싶다면:
#   메모리 부족 에러를 방지하기 위해 /boot/firmware/config.txt 파일 하단에 "dtoverlay=vc4-kms-v3d,cma-256" 줄을 적고 리부팅하십시오.
#   그 후 아래 x264enc 라인을 지우고 하드웨어 명령 코드를 기입하면 CPU 점유율이 더욱 낮아집니다.
#   (예: v4l2h264enc extra-controls="controls,h264_i_frame_period=30")

gst-launch-1.0 -e \
    libcamerasrc ! \
    video/x-raw,width=${RESOLUTION_WIDTH},height=${RESOLUTION_HEIGHT},framerate=${FPS}/1 ! \
    videoconvert ! \
    x264enc tune=zerolatency speed-preset=ultrafast bitrate=${VIDEO_BITRATE} key-int-max=15 bframes=0 threads=4 sliced-threads=true ! \
    h264parse config-interval=1 ! \
    tee name=t \
    t. ! queue max-size-buffers=150 max-size-time=0 max-size-bytes=0 ! \
       splitmuxsink location="${BACKUP_DIR}/backup_${START_TIME}_%05d.ts" max-size-time=10000000000 async-handling=true \
    t. ! queue leaky=downstream max-size-buffers=60 max-size-time=0 max-size-bytes=0 ! \
       flvmux streamable=true ! \
       queue leaky=downstream max-size-buffers=60 max-size-time=0 max-size-bytes=0 ! \
       rtmpsink location="rtmp://127.0.0.1:1935/${RTSP_PATH}"

# === 각 GStreamer 엘리먼트 라인 한글 기능 설명 ===
#
# 1. libcamerasrc : 라즈베리파이의 공식 카메라 하드웨어 인터페이스로부터 원본 프레임을 획득
# 2. video/x-raw,width=1920,height=1080,framerate=30/1 : 영상 규격을 가로 1920, 세로 1080, 초당 30프레임
# 3. videoconvert : 카메라 원시 색상 데이터를 압축 인코더 코덱이 이해할 수 있도록 공용 변환 처리
# 4. x264enc : H.264 압축 코덱
#      - tune=zerolatency: 인코딩 대기 프레임 누적을 없애 실시간성을 보장
#      - speed-preset=ultrafast: 가장 빠른 연산 속도로 압축하여 CPU 발열을 낮춤
#      - bitrate=${VIDEO_BITRATE}: 1080p 화면 깨짐을 줄이기 위해 영상 전송률을 4000kbps 기준으로 설정합니다.
#      - key-int-max=15: 15프레임(약 0.5초)마다 강제로 핵심 I-프레임을 삽입하여 재접속 시 빠르게 화면이 나타나게 합니다.
#      - bframes=0: 압축 지연을 유발하는 화면 예측 프레임(B-Frame)을 0개로 비활성화하여 레이턴시를 죽입니다.
#      - threads=4 sliced-threads=true: 라즈베리파이 4의 쿼드코어 CPU 전체에 인코딩 연산 짐을 병렬로 분산하여 단일 코어 과부하 렉을 근절합니다.
# 병렬연산 안하면 진짜 무조건 버벅댐. 라즈베리파이4 성능으로는.
# 5. h264parse config-interval=1 : 압축된 데이터의 파싱 템플릿입니다. config-interval=1은 매 1초마다 SPS/PPS 복원 규격 헤더를 강제 반복 삽입하여 PC가 재연결될 때 곧바로 화면 해석을 시작할 수 있게 만듭니다.
# 6. tee name=t : 가공 완료된 압축 비디오 배달 상자 컨베이어 벨트를 2갈래로 평행 분할하는 분기 장치입니다.
#
# --- [분기 1: 로컬 저장 백업] ---
# 7. queue max-size-buffers=150 ... : 파일로 기록될 대용량 데이터를 안전하게 잠시 임시 대기시키는 넉넉한 큐 버퍼입니다.
# 8. splitmuxsink : 비디오 파일을 일정 시간 규격으로 자동 분할 저장하는 복합 파일 싱크입니다.
#      - location=.../backup_..._%05d.ts: 생성되는 파일명 뒤에 5자리 카운트 숫자(%05d)를 붙여줍니다.
#      - max-size-time=10000000000: 나노초 기준 10,000,000,000ns 즉, 10초(10s) 단위로 파일을 쪼갭니다.
#      - muxer=mpegtsmux: MPEG-TS 포맷을 사용하여 파일 닫기 시점의 디스크 쓰기 병목 부하(I/O Peak)를 거의 0%로 상쇄합니다.
#      - async-handling=true: 파일 쪼개기 작업 시 생기는 찰나의 파일 시스템 지연이 실시간 송출 컨베이어 벨트에 간섭(대기 유발)하지 않도록 별도의 비동기 독립 스레드에서 차단 격리합니다.
#
# --- [분기 2: 실시간 송출] ---
# 9. queue leaky=downstream max-size-buffers=60 : 네트워크가 일시 정체되어 패킷이 전송 지연되면, 전체 카메라가 굳지 않도록 오래된 프레임을 즉시 버리고 흘려보내는 누수형(Leaky) 고속 네트워크 큐 버퍼입니다.
# 10. flvmux streamable=true : RTSP 변환 이전에 가벼운 스트리밍 컨테이너인 RTMP 규격으로 실시간 비디오 신호를 재포장합니다.
# 11. rtmpsink location=rtmp://127.0.0.1:1935/live : 로컬 백그라운드에서 실행 대기 중이던 MediaMTX 1935번 포트로 RTMP 패킷을 연속 덤핑 사출하여 RTSP 신호 변환을 완수시킵니다.
