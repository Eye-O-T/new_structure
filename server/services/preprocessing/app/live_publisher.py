"""Coalesce live detections so slow API calls never backlog video frames."""

import queue
import threading


class LivePublisher:
    def __init__(self, data_client, camera_id):
        self.data = data_client
        self.camera_id = camera_id
        self.pending = queue.Queue(maxsize=1)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def submit(self, payload):
        try:
            self.pending.put_nowait(payload)
        except queue.Full:
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
            try:
                self.data.put_live_objects(self.camera_id, payload)
            except Exception:
                pass  # Frames are ephemeral; API consumers discard stale boxes.

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=3)
