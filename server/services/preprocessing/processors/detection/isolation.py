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
        runtime = tracker.runtime_metadata() if callable(getattr(tracker, "runtime_metadata", None)) else None
        connection.send({"kind": "ready", "runtime": runtime})
        while True:
            operation, frame = connection.recv()
            try:
                if operation == "reset":
                    tracker.reset()
                    result = None
                elif operation == "process":
                    started = time.monotonic()
                    result = DetectionResult.model_validate(
                        tracker.process(frame)
                    ).model_dump(mode="json")
                    model_seconds = max(0, time.monotonic() - started)
                else:
                    raise ValueError("Invalid detection operation")
                response = {"kind": "result", "value": result}
                if operation == "process":
                    response["model_seconds"] = model_seconds
                connection.send(response)
            except Exception:
                # 모델 예외의 원문에 경로·인증 정보를 포함하지 않는다.
                connection.send({"kind": "error"})
    except (EOFError, BrokenPipeError, OSError):
        pass
    except Exception as exc:
        try:
            message = str(exc).lower()
            if "PAR attribute bundles" in str(exc):
                error_code = "MODEL_FORMAT_INVALID"
            elif str(arguments[2]).startswith("cuda"):
                try:
                    import torch
                    cuda_build_missing = torch.version.cuda is None
                except Exception:
                    cuda_build_missing = False
                if cuda_build_missing or "not compiled with cuda" in message:
                    error_code = "GPU_CUDA_BUILD_MISSING"
                elif "cuda" in message:
                    error_code = "GPU_DEVICE_UNAVAILABLE"
                else:
                    error_code = "DETECTION_STARTUP_FAILED"
            else:
                error_code = "DETECTION_STARTUP_FAILED"
            connection.send({"kind": "startup_error", "error_code": error_code})
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
        self.runtime = None
        self.last_model_seconds = None
        self.last_ipc_seconds = None
        self._process = context.Process(
            target=_serve,
            args=(child, reference, (model_path, confidence, device)),
            daemon=True,
        )
        try:
            self._process.start()
            child.close()
            ready = self._exchange(startup_timeout_seconds)
            if ready.get("kind") != "ready":
                code = ready.get("error_code")
                if code not in {
                    "MODEL_FORMAT_INVALID", "DETECTION_STARTUP_FAILED",
                    "GPU_CUDA_BUILD_MISSING", "GPU_DEVICE_UNAVAILABLE",
                }:
                    code = "DETECTION_STARTUP_FAILED"
                raise RuntimeError(code)
            self.runtime = ready.get("runtime")
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
                started = time.monotonic()
                result = self._exchange(self.timeout_seconds, (operation, frame))
                if result.get("kind") != "result":
                    raise RuntimeError("DETECTION_MODEL_FAILED")
                if operation == "process":
                    self.last_model_seconds = result.get("model_seconds")
                    self.last_ipc_seconds = max(
                        0, time.monotonic() - started - (self.last_model_seconds or 0)
                    )
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
