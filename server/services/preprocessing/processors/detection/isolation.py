"""카메라별 감지 모델을 종료 가능한 자식에 두어 네이티브 교착을 회복한다."""

import multiprocessing
import threading
import time

from ai_cctv_core.processing.plugins import load_factory

from .contracts import DetectionResult


def _serve(connection, reference, arguments):
    try:
        tracker = load_factory(reference)(*arguments)
        if not all(
            callable(getattr(tracker, name, None)) for name in ("reset", "process")
        ):
            raise ValueError("Invalid detection processor")
        connection.send({"kind": "ready"})
        while True:
            operation, frame = connection.recv()
            try:
                if operation == "reset":
                    tracker.reset()
                    result = None
                elif operation == "process":
                    result = DetectionResult.model_validate(
                        tracker.process(frame)
                    ).model_dump(mode="json")
                else:
                    raise ValueError("Invalid detection operation")
                connection.send({"kind": "result", "value": result})
            except Exception:
                # 모델 예외의 원문에 경로·인증 정보를 포함하지 않는다.
                connection.send({"kind": "error"})
    except (EOFError, BrokenPipeError, OSError):
        pass
    except Exception:
        try:
            connection.send({"kind": "startup_error"})
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        connection.close()


class IsolatedDetector:
    """시간 초과한 모델은 종료한다. 다음 세션의 생성은 CameraWorker가 담당한다."""

    def __init__(
        self,
        reference,
        model_path,
        confidence,
        device,
        *,
        timeout_seconds=10,
        startup_timeout_seconds=30,
        stop_event=None,
    ):
        self.timeout_seconds = timeout_seconds
        self._stop = stop_event if stop_event is not None else threading.Event()
        self._lifecycle = threading.RLock()
        self._call_lock = threading.Lock()
        self._closed = False
        self._io_thread = None
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self._connection = parent
        self._process = context.Process(
            target=_serve,
            args=(child, reference, (model_path, confidence, device)),
            daemon=True,
        )
        try:
            self._process.start()
            child.close()
            if self._exchange(startup_timeout_seconds).get("kind") != "ready":
                raise RuntimeError("DETECTION_STARTUP_FAILED")
        except BaseException:
            child.close()
            self.close()
            raise

    def _exchange(self, timeout, message=None):
        # Pipe.send와 poll 이후 recv도 큰 프레임·불완전한 응답에서 블로킹할 수 있다.
        # I/O는 별도 스레드에 두어 전송부터 수신 완료까지 같은 제한으로 감독한다.
        deadline = time.monotonic() + timeout
        connection = self._connection
        completed = threading.Event()
        result = []
        errors = []

        def exchange():
            try:
                if message is not None:
                    connection.send(message)
                result.append(connection.recv())
            except Exception as error:
                errors.append(error)
            finally:
                completed.set()

        with self._lifecycle:
            if self._closed or self._stop.is_set():
                raise RuntimeError("DETECTION_STOPPED")
            self._io_thread = threading.Thread(
                target=exchange, name="detection-ipc", daemon=True
            )
            self._io_thread.start()
        while True:
            if self._closed or self._stop.is_set():
                raise RuntimeError("DETECTION_STOPPED")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("DETECTION_MODEL_TIMEOUT")
            if completed.wait(min(0.1, remaining)):
                if errors:
                    raise RuntimeError("DETECTION_PROCESS_UNAVAILABLE") from None
                return result[0]

    def _request(self, operation, frame=None):
        with self._call_lock:
            try:
                if self._closed or self._stop.is_set():
                    raise RuntimeError("DETECTION_STOPPED")
                result = self._exchange(self.timeout_seconds, (operation, frame))
                if result.get("kind") != "result":
                    raise RuntimeError("DETECTION_MODEL_FAILED")
                return result["value"]
            except BaseException:
                self.close()
                raise

    def reset(self):
        self._request("reset")

    def process(self, frame):
        return DetectionResult.model_validate(self._request("process", frame))

    def close(self):
        # 추론을 기다리는 카메라 스레드와 종료 요청이 같은 핸들을 닫지 않게 한다.
        with self._lifecycle:
            self._closed = True
            connection, process = self._connection, self._process
            self._connection = None
            if process is not None and process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=1)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1)
                if process.is_alive():
                    raise RuntimeError("DETECTION_PROCESS_DID_NOT_STOP")
            if connection is not None:
                connection.close()
            if self._io_thread is not None:
                self._io_thread.join(timeout=1)
            if process is not None:
                process.close()
            self._process = None
