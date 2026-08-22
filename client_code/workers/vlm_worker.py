# vlm_worker.py

import threading
import queue
import gc

class VLMWorker:
    def __init__(self, state_manager):
        self.state_manager = state_manager
        self.task_queue = queue.Queue()
        self.running = False
        self.thread = None
        self.analyzer = None
        self.ready_event = threading.Event()
        self.failed_event = threading.Event()
        self.error_message = None

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            print("VLM Worker 이미 실행 중")
            return

        self.ready_event.clear()
        self.failed_event.clear()
        self.error_message = None
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def is_ready(self):
        return self.ready_event.is_set()

    def has_failed(self):
        return self.failed_event.is_set()

    def wait_until_ready(self, timeout=0.1):
        return self.ready_event.wait(timeout=timeout)

    def add_task(self, person_id, crop_path):
        if not self.running:
            return

        self.task_queue.put((person_id, crop_path))

    def _run(self):
        try:
            print("VLM 모델 로딩 중...")
            from vlm.person_analyzer import PersonAnalyzer

            self.analyzer = PersonAnalyzer()
            print("VLM 모델 로딩 완료")
            self.ready_event.set()
        except Exception as e:
            print(f"VLM 모델 로딩 실패: {e}")
            self.error_message = str(e)
            self.failed_event.set()
            self.running = False
            return

        while self.running:
            try:
                person_id, crop_path = self.task_queue.get(timeout=1)
            except queue.Empty:
                continue

            try:
                print(f"ID {person_id} VLM 분석 시작: {crop_path}")

                result = self.analyzer.analyze(crop_path)

                self.state_manager.mark_vlm_done(person_id, result)

                print(f"ID {person_id} VLM 분석 결과:")
                print(result)
                from chat_bot import chat_bot as chatbot

                chatbot.send_msg(f"ID {person_id}\n{result}")
                
            except Exception as e:
                print(f"ID {person_id} VLM 분석 실패: {e}")

            finally:
                self.task_queue.task_done()

        self.cleanup()

    def stop(self):
        self.running = False

        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=10)

        self.cleanup()

    def cleanup(self):
        if self.analyzer is not None:
            try:
                del self.analyzer
            except Exception:
                pass

            self.analyzer = None

        gc.collect()

        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
