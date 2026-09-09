# 실시간 박스는 오래된 자료를 모두 보내기보다 가장 최근 상태를 빨리 보내는 것이 중요하다.
# HTTP 전송을 별도 스레드에 맡겨 느린 네트워크가 카메라 탐지를 막지 않게 한다.

import queue
import threading


# 최신 좌표 한 건만 보관하는 비동기 전송기로 탐지 스레드의 네트워크 대기를 줄인다.
class LivePublisher:
    def __init__(self, data_client, camera_id):
        self.data = data_client
        self.camera_id = camera_id
        # 대기 공간을 한 칸만 두고, 전송 중 새 결과가 쌓이면 가장 최신 결과만 남긴다.
        self.pending = queue.Queue(maxsize=1)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    # 생산자를 기다리게 하지 않고 전송 대기 중인 좌표를 최신 관측으로 교체한다.
    def submit(self, payload):
        if self.stopped.is_set():
            return
        try:
            self.pending.put_nowait(payload)
        except queue.Full:
            # 아직 전송하지 않은 이전 좌표를 버린 뒤 새 좌표로 교체한다.
            try:
                self.pending.get_nowait()
            except queue.Empty:
                pass
            try:
                self.pending.put_nowait(payload)
            except queue.Full:
                pass

    def run(self):
        while not self.stopped.is_set():
            try:
                payload = self.pending.get(timeout=0.5)
            except queue.Empty:
                continue
            if self.stopped.is_set():
                break
            try:
                self.data.put_live_objects(self.camera_id, payload)
            except Exception:
                pass  # 좌표는 일시적인 상태다. 소비자는 오래된 박스를 버리고 다음 갱신을 기다린다.

    # 종료를 요청하고 제한 시간만 기다린다. 남은 좌표를 모두 전송하는 보장은 하지 않는다.
    def close(self):
        self.stopped.set()
        self.thread.join(timeout=3)
