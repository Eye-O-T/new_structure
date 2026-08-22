import gi
import sys

#gstreamer 라이브러리 사용
gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import Gst, GstRtspServer, GLib

class CameraRTSPServer:
    def __init__(self):
        Gst.init(None) #gstreamer 설정 초기화

        #rtsp 서버객체 만들기
        self.server = GstRtspServer.RTSPServer()
        self.server.set_service("8554")

        factory = GstRtspServer.RTSPMediaFactory() #어떻게 전송할지 정의하는 객체
        factory.set_shared(True) #여러 클라이언트 접속허용

        pipeline = (#어떻게 전송할지 정하기
            "( "
            "libcamerasrc ! "#파이카메라
            "video/x-raw,width=640,height=480,framerate=30/1 ! "
            "videoconvert ! "#압축형태에 맞춰서 색상/형식 변환
            "x264enc tune=zerolatency speed-preset=ultrafast bitrate=1000 ! " #h264,지연 낮추고 화질포기, 압축속도 최고로,비트레이트 설정.
            "h264parse ! "#h264로 압축된거에 헤더붙여줌. 수신을 위해 필수
            "rtph264pay config-interval=1 name=pay0 pt=96 "#실시간 전송으로 h264포맷으로 payload한다.
            #config-interval=1 > 해독설명서를 1초마다 보냄
            #name > rtsp 식별자
            #pt > payload type 96은 동적 타입으로 h264에 자주 사용됨
            ")"
        )

        factory.set_launch(pipeline)#정한걸 등록

        #서버 만들기. mount 하나 만듬. 이름은 /stream
        #카메라 추가할경우 mount 더 만들면됨.
        mounts = self.server.get_mount_points()
        mounts.add_factory("/stream", factory)

        self.server.attach(None) #서버시작

        print("RTSP 서버 시작")

if __name__ == "__main__":
    server = CameraRTSPServer()
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")
        sys.exit(0)