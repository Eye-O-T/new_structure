import cv2
import threading  # 멀티스레딩 씀. 영상 수신 스레드와 감시(Watchdog) 스레드를 병렬로 실행할려고
import time  # 시간 계측 및 지연(sleep) 관리
import sys  # 시스템 출력 콘솔 제어 (대기 메시지 출력용)
import os  # 운영체제 환경변수 설정용 (FFmpeg 옵션 주입
import socket  # 네트워크 소켓 통신: 서버의 포트가 살아있는지 사전 노크(TCP Handshake)용.
from urllib.parse import urlparse  # URL 파싱: RTSP 주소에서 IP와 포트 번호를 분리 추출하기 위함

# 핵심 설정-OpenCV의 비디오 디코더 백엔드(FFmpeg)에 TCP 전송 방식 강제 및 2초 타임아웃 주입
# - rtsp_transport;tcp: 패킷 손실이 없는 신뢰성 있는 TCP 프로토콜을 강제 사용
# - stimeout/timeout;2000000: 연결 및 데이터 수신 타임아웃을 2초(2백만 마이크로초)로 제안
# - 주의: 옵션 값들은 반드시 세미콜론(;)으로 구분되어야 FFmpeg C++ 라이브러리가 올바르게 파싱함
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp;stimeout;2000000;timeout;2000000"


#rtsp 주소 읽어서 tcp로 포트 열려있는지 확인하는 함수.
def check_server_port_open(rtsp_url, timeout=1.5): #tcp이용해서 rtsp 빨리 재연결할 수 있게함. 이거 없이 rtsp로만하면 최소 재연결에 10초넘게걸림
    """
    RTSP 서버의 TCP 포트(기본 8554)가 현재 물리적으로 열려 있는지 사전에 노크해 보는 함수.
    이 함수를 먼저 거침으로써, 서버가 꺼져 있을 때 OpenCV 비디오 캡처가 30초 동안 굳어버리는 치명적인 문제를 방지합니다.
    """
    try:
        # 1. 입력받은 RTSP URL 주소를 구조별로 분해 (예: rtsp://192.168.99.200:8554/live)
        parsed = urlparse(rtsp_url)
        hostname = parsed.hostname #192.168.99.200 같은 ip만 떼냄
        port = parsed.port if parsed.port is not None else 8554 #8554 포트만 떼냄
        
        # 만약 urlparse가 올바르게 주소를 잘라내지 못했을 때를 대비한 예외용 백업 파싱 로직
        if not hostname:
            clean_url = rtsp_url.replace("rtsp://", "")
            host_port = clean_url.split("/")[0]
            if ":" in host_port:
                hostname, port_str = host_port.split(":")
                port = int(port_str)
            else:
                hostname = host_port
                port = 8554
                
        # 2. 아주 가볍고 빠른 TCP 소켓 통신을 시도합니다. = 노트하는거임.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)  # 1.5초 안에 응답이 없으면 즉시 끊고 나옵니다.
        sock.connect((hostname, port))  # 연결 시도 (포트가 열려 있으면 통과, 닫혀 있으면 에러 발생)
        sock.close()  # 연결 성공 즉시 소켓을 안전하게 닫아줍니다.
        return True  # 서버 포트가 정상 작동 중임을 알림
    except Exception:
        return False  # 접속 불가 상태 (서버 오프라인 또는 네트워크 단선)


class RTSPReceiver:
    """
    라즈베리파이 RTSP 스트림을 백그라운드 스레드에서 수신하고, 
    단선 감지 시 자동으로 재연결을 시도하며, 영상이 굳었을 때 리소스를 강제 회수하는 감시견(Watchdog)을 탑재한 클래스.
    """
    def __init__(self, rtsp_url, reconnect_interval=3):
        self.rtsp_url = rtsp_url  # 접속할 RTSP 주소
        self.reconnect_interval = reconnect_interval  # 접속 실패 시 다시 시도할 대기 간격 (초 단위)
        
        self.frame = None  # 수집된 최신 영상 프레임이 임시로 담기는 보관함
        self.is_connected = False  # 현재 스트림 연결 여부 플래그
        self.running = False  # 전체 수신기 클래스의 가동 여부 제어 플래그
        self.thread = None  # 영상을 계속 받아오는 전용 스레드 객체
        self.watchdog_thread = None  # 영상 프리징을 실시간 감시하는 스레드 객체
        self.last_frame_time = time.time()  # 마지막으로 프레임이 정상 유입된 시간 (타임스탬프)
        self.cap = None  # OpenCV의 cv2.VideoCapture 인스턴스 저장 변수
        
        # 스레드 동기화 락(Lock) 화면갱신 스레드랑 비디오캡처 스레드가 같은 전역메모리 써서 락필요함.
        # - 여러 스레드가 동시에 self.frame이나 self.cap에 접근할 때 데이터 충돌(Race Condition)이 나는 것을 막아주는 안전 잠금 장치
        self.lock = threading.Lock()

    def start(self):
        """
        수신기와 watchdog 백그라운드 스레드를 개시하는 함수.
        """
        self.running = True
        self.last_frame_time = time.time()  # 초기 감시 기준 시간 설정
        
        # 1. 실시간 영상 수신 루프 실행 스레드 생성 및 시작
        self.thread = threading.Thread(target=self._receive_loop, daemon=True)
        self.thread.start()
        
        # 2. 영상 프리징 감시 루프 실행 스레드 생성 및 시작
        self.watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self.watchdog_thread.start()
        print(f"[*] RTSP Receiver thread and Watchdog started for: {self.rtsp_url}")

    def _watchdog_loop(self): #tcp노크 성공해서 연결했는데 영상 안들어오는 경우 다수. 그러면 30초 굳어버림. cv2비디오캡처가 그럼. 그래서 5초 이상 영상 안들어오면 소켓 해제.
        """
        RTSP 연결은 성공했으나, 소켓 장애로 인해 내부적으로 프레임 공급이 5초 이상 중단되었을 때, 
        무한 대기 상태(Hang)를 깨부수고 리소스를 강제 릴리즈하여 재연결을 강제 유도하는 감시 루프.
        """
        while self.running:
            time.sleep(1.0)  # 너무 자주 감시하면 CPU가 낭비되므로 1초 주기로 체크
            if self.is_connected:
                # 5.0초 동안 한 장의 사진도 들어오지 않았다면 영상이 동결(Freeze)된 것으로 간주
                if time.time() - self.last_frame_time > 5.0:
                    print("[!] Watchdog: Stream freeze detected. Forcing resource release...")
                    with self.lock:
                        if self.cap is not None:
                            self.cap.release()  # 락이 걸린 OpenCV 소켓을 물리적으로 강제 파괴하여 대기 상태를 강제 중단

    def _receive_loop(self):#무한 rtsp 수신.
        """
        [영상 수신 및 재연결 내부 스레드]
        네트워크가 정전/단선되어도 끊임없이 복구를 타진하며 프레임을 최신 상태로 유지하는 메인 핵심 루프.
        """
        was_unreachable = True  # 이전 상태가 접속 불가능했는지 추적하는 플래그 (최초 접속도 불가능으로 간주)
        while self.running:
            print(f"[i] Attempting to connect to RTSP server: {self.rtsp_url}")
            
            # 1. 1차 포트 노크 단계: 30초 프리징 방지를 위한 TCP 노크
            if not check_server_port_open(self.rtsp_url, timeout=1.5):
                print(f"[!] RTSP Server port is unreachable. Retrying in {self.reconnect_interval} seconds...")
                was_unreachable = True  # 단선 상태 기록
                time.sleep(self.reconnect_interval)  # 3초 대기 후 처음부터 다시 포트 체크
                continue
                
            # 2. 물리 결합 안정화 단계: 랜선 탈착 시 이더넷 카드 및 IP 라우팅 테이블 안정화를 위해 1.5초 대기
            if was_unreachable:
                print("[~] RTSP Server port is open. Waiting 1.5s for network link to stabilize...")
                time.sleep(1.5)  # OS가 네트워크 구성을 마칠 때까지 안정화 대기 시간 부여
                was_unreachable = False  # 이제 네트워크가 정상화됨을 기록
                
            # 3. OpenCV 수신기 인스턴스 생성 (포트가 열려있음이 확실하므로 단번에 즉각 열립니다)
            cap = cv2.VideoCapture(self.rtsp_url)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # 비디오 버퍼 크기를 1로 제한하여 지연을 최소화

            with self.lock:
                self.cap = cap
                self.last_frame_time = time.time()

            # VideoCapture 오픈에 실패했을 경우 예외 처리
            if not cap.isOpened():
                print(f"[!] Failed to open RTSP stream. Retrying in {self.reconnect_interval} seconds...")
                cap.release()
                with self.lock:
                    self.cap = None
                time.sleep(self.reconnect_interval)
                continue

            print("[+] Successfully connected to RTSP stream.")

            # 4. 동적 소켓 버퍼 플러싱 (비활성화): ?????? 이해못함.
            # - H.264 디코더가 화면 복원에 필수적인 키프레임(I-Frame) 헤더 정보를 잃지 않도록 수동 플러싱(cap.grab)을 비활성화합니다.
            # - 대신 OpenCV FFMPEG 백엔드가 버퍼 큐를 순차적으로 동기화하도록 유도하여 초기 디코딩 실패 확률을 없앤다.
            print("[i] Skipping buffer flush to prevent keyframe loss for H.264 decoder.")
                 
            self.is_connected = True
            self.last_frame_time = time.time()

            # 5. 정상 스트리밍 데이터 수집 루프
            # - 일시적인 패킷 손실이나 키프레임(I-Frame) 누락으로 인한 단발성 디코딩 에러를 허용하도록 오차 허용 설계 적용.
            # - 사용자 피드백에 의해 연속 실패 임계치를 80프레임(약 2.6초)으로 상향 튜닝하여 초기 접속/재접속 안정성을 확보했습니다.
            consecutive_failures = 0 #프레임 수신실패 횟수 카운트.
            while self.running:
                ret, frame = cap.read()  # 프레임 취득 및 디코딩 (ret: 성공 여부, frame: 비디오 이미지 numpy 배열)
                if not ret:
                    consecutive_failures += 1
                    if consecutive_failures >= 80:
                        print("[!] Frame read failed consecutively 80 times. Connection might be lost. Reconnecting...")
                        break
                    time.sleep(0.01)  # CPU 독점을 예방하기 위해 아주 미세한 쉬는 시간 부여
                    continue
                
                consecutive_failures = 0  # 프레임 읽기 성공 시 실패 카운트 즉시 리셋
                self.last_frame_time = time.time()  # 감시용 타임스탬프 갱신
                with self.lock:
                    self.frame = frame.copy()  # 다른 스레드가 안전하게 가져갈 수 있도록 원본 데이터를 복제해서 보관. 이렇게 안하면 화면 깨질 수 있다함.
            
            # 6. 소켓 에러 및 수신기 정지 시 리소스 안전 해제
            self.is_connected = False
            cap.release()
            with self.lock:
                self.cap = None
            
            if self.running:
                time.sleep(self.reconnect_interval)  # 루프 복귀 전 3초 휴식

    def get_frame(self):
        """
        GUI 대시보드나 AI 추론 모델이 메인 루프에서 호출하는 외부용 함수.
        - 스레드 세이프(Thread-Safe)하게 락을 획득한 후 수집된 최신 이미지의 사본을 안전하게 꺼내줍니다.
        """
        with self.lock:
            if self.frame is not None:
                return self.frame.copy()  # 원본 이미지의 복제본(사본)을 전달하여 메모리 충돌 방지
            return None

    def stop(self):
        """
        프로그램 종료 시 외부에서 안전하게 수집 전담 스레드와 감시 스레드를 정지시키는 해제 함수.
        """
        self.running = False  # 스레드 종료 트리거
        if self.thread is not None:
            self.thread.join(timeout=3)  # 수집 스레드가 완전히 퇴근할 때까지 최대 3초 대기
        if self.watchdog_thread is not None:
            self.watchdog_thread.join(timeout=3)  # 감시 스레드가 완전히 퇴근할 때까지 최대 3초 대기
        print("[*] RTSP Receiver stopped.")


def main():
    # 라즈베리파이의 실제 이더넷 IP 주소 및 RTSP 송출 경로 입력
    RTSP_URL = "rtsp://192.168.99.200:8554/live"
    
    # RTSP 수신기 객체 생성 및 백그라운드 구동 개시
    receiver = RTSPReceiver(rtsp_url=RTSP_URL, reconnect_interval=3)
    receiver.start()
    
    print("\n------------------------------------------------")
    print("Press 'q' in the OpenCV window to exit safely.")
    print("------------------------------------------------\n")
    
    try:
        while True:
            # 백그라운드 수신 스레드 보관함에서 최신 프레임을 획득
            frame = receiver.get_frame()
            
            if frame is not None:
                # ------------------------------------------------------------------
                # TODO: 향후 여기에 YOLOv8 객체 탐지 및 안면인식 추론 로직 연동 예정
                # 예: results = model(frame) -> bbox 그리기
                # ------------------------------------------------------------------
                
                # ------------------------------------------------------------------
                # TODO: 향후 여기에 Tkinter GUI 대시보드 이벤트 루프 연동 예정
                # ------------------------------------------------------------------
                
                # OpenCV 윈도우 창을 띄워 PC 화면에 실시간 라이브 영상 렌더링 출력
                cv2.imshow("Network Video Stream (PC Client)", frame)
            else:
                # 프레임이 아직 준비되지 않았거나 연결이 끊어졌을 때 CPU 점유율 과부하(무한 스핀)를 예방하고
                # 콘솔 화면에 대기중이라는 피드백 출력 제공
                sys.stdout.write("\r[~] Waiting for RTSP stream frames...")
                sys.stdout.flush()
                time.sleep(0.1)
                continue
                
            # 화면 표시창 위에서 키보드 'q' 키를 누르면 루프를 빠져나와 정상 종료 절차 밟기
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
                
    except KeyboardInterrupt:
        print("\n[!] Program interrupted by user.")  # Ctrl+C 중단 발생 시 로그 출력
    finally:
        # 프로그램이 죽거나 종료될 때 점유하고 있던 윈도우 창 및 소켓 리소스를 영구 해제
        receiver.stop()
        cv2.destroyAllWindows()
        print("[+] Resources released. Client exited.")

if __name__ == "__main__":
    main()
