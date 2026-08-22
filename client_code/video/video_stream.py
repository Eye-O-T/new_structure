# video_stream.py

import cv2
import time
from video.rtsp_receiver import RTSPReceiver


class VideoStream:
    def __init__(self, source=0, max_width=1280, max_height=720, target_fps=24):
        self.source = source
        self.cap = None
        self.receiver = None
        self.max_width = max_width
        self.max_height = max_height
        self.target_fps = target_fps
        
        # 입력 소스가 rtsp:// 로 시작하는 문자열인지 확인
        self.is_rtsp = isinstance(self.source, str) and self.source.lower().startswith("rtsp://")
        
        # RTSP 모드에서 프레임 중복 방지 및 동기화를 위한 마지막 읽은 프레임 타임스탬프
        self.last_read_frame_time = None

    def open(self):
        if self.is_rtsp:
            print(f"[VideoStream] RTSP 모드 활성화 - 소스: {self.source}")
            self.receiver = RTSPReceiver(rtsp_url=self.source, reconnect_interval=3)
            self.receiver.start()
            return True
        else:
            self.cap = cv2.VideoCapture(self.source)
            if not self.cap.isOpened():
                print("영상 스트림 연결 실패")
                return False
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.max_width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.max_height)
            self.cap.set(cv2.CAP_PROP_FPS, self.target_fps)
            print("영상 스트림 연결 성공")
            return True

    def read(self):
        if self.is_rtsp:
            if self.receiver is None:
                return False, None
            
            # 새로운 프레임이 수신될 때까지 미세 대기 (최대 100ms)하여 CPU 공회전 방지 및 동기화 수행
            start_wait = time.time()
            while True:
                # 수신기의 최신 프레임 갱신 시점 확인
                last_time = getattr(self.receiver, "last_frame_time", 0)
                
                # 프레임이 존재하고, 이전 읽은 시점보다 새로운 프레임인 경우 루프 탈출
                if self.receiver.frame is not None and (self.last_read_frame_time is None or last_time > self.last_read_frame_time):
                    break
                
                # 5ms 대기 후 재검사
                time.sleep(0.005)
                
                # 연결이 완전히 끊겼거나 100ms 대기 시 타임아웃으로 탈출
                if not self.receiver.is_connected or (time.time() - start_wait > 0.1):
                    break
            
            current_frame_time = getattr(self.receiver, "last_frame_time", 0)
            if (
                self.last_read_frame_time is not None
                and current_frame_time <= self.last_read_frame_time
            ):
                return False, None

            frame = self.receiver.get_frame()
            if frame is None:
                return False, None
            
            self.last_read_frame_time = current_frame_time
            return True, self._resize_to_profile(frame)
        else:
            if self.cap is None:
                return False, None
            ret, frame = self.cap.read()
            if not ret:
                return False, None
            return True, self._resize_to_profile(frame)

    def get_fps(self):
        if self.is_rtsp:
            # RTSP 수신기 내 OpenCV cap 객체에서 FPS 획득 시도
            fps = 30
            if self.receiver is not None:
                with self.receiver.lock:
                    if self.receiver.cap is not None:
                        fps = self.receiver.cap.get(cv2.CAP_PROP_FPS)
            if fps <= 0:
                return self.target_fps
            return min(fps, self.target_fps)
        else:
            if self.cap is None:
                return self.target_fps
            fps = self.cap.get(cv2.CAP_PROP_FPS)
            if fps <= 0:
                return self.target_fps
            return min(fps, self.target_fps)

    def get_frame_size(self):
        if self.is_rtsp:
            # 수신된 최신 프레임의 크기를 직접 분석
            if self.receiver is not None:
                frame = self.receiver.get_frame()
                if frame is not None:
                    h, w = frame.shape[:2]
                    return w, h
            return 640, 480
        else:
            if self.cap is None:
                return 640, 480
            width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            return self._scaled_size(width, height)

    def _resize_to_profile(self, frame):
        height, width = frame.shape[:2]
        scaled_width, scaled_height = self._scaled_size(width, height)

        if scaled_width == width and scaled_height == height:
            return frame

        return cv2.resize(
            frame,
            (scaled_width, scaled_height),
            interpolation=cv2.INTER_AREA,
        )

    def _scaled_size(self, width, height):
        if width <= 0 or height <= 0:
            return self.max_width, self.max_height

        scale = min(self.max_width / width, self.max_height / height, 1.0)
        scaled_width = int(width * scale)
        scaled_height = int(height * scale)

        if scaled_width % 2 == 1:
            scaled_width -= 1
        if scaled_height % 2 == 1:
            scaled_height -= 1

        return max(2, scaled_width), max(2, scaled_height)

    def pop_connection_events(self):
        if self.is_rtsp and self.receiver is not None:
            return self.receiver.pop_connection_events()
        return []

    def release(self):
        if self.is_rtsp:
            if self.receiver is not None:
                self.receiver.stop()
                self.receiver = None
        else:
            if self.cap is not None:
                self.cap.release()
                self.cap = None

