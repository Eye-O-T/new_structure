"""카메라별 감지 모델을 종료 가능한 자식에 두어 네이티브 교착을 회복한다."""

import math
import multiprocessing
import re
import threading
import time

from ai_cctv_core.processing.plugins import load_factory

from .contracts import DetectionResult

_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_ERROR_TYPE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_ALLOWED_STAGES = {"startup", "reset", "process"}
_STARTUP_ERROR_CODES = {
    "MODEL_FORMAT_INVALID",
    "DETECTION_STARTUP_FAILED",
    "GPU_CUDA_BUILD_MISSING",
    "GPU_DEVICE_UNAVAILABLE",
}


def _safe_error_code(value, default="DETECTION_MODEL_FAILED"):
    if isinstance(value, str) and _ERROR_CODE_PATTERN.fullmatch(value):
        return value
    return default


def _safe_error_type(value, default="RuntimeError"):
    if isinstance(value, str) and _ERROR_TYPE_PATTERN.fullmatch(value):
        return value
    return default


def _safe_stage(value, default="process"):
    return value if isinstance(value, str) and value in _ALLOWED_STAGES else default


def _safe_elapsed_seconds(value):
    if (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    ):
        return float(value)
    return None


def _classify_runtime_error(exc, operation):
    explicit = getattr(exc, "error_code", None)
    if isinstance(explicit, str) and _ERROR_CODE_PATTERN.fullmatch(explicit):
        return explicit
    type_name = type(exc).__name__
    message = str(exc).lower()
    if "ValidationError" in type_name:
        return "DETECTION_VALIDATION_FAILED"
    if "out of memory" in message:
        return "GPU_OUT_OF_MEMORY"
    if "cuda" in message:
        return "GPU_DEVICE_UNAVAILABLE"
    if operation == "reset":
        return "DETECTION_RESET_FAILED"
    return "DETECTION_MODEL_FAILED"


class DetectionProcessError(RuntimeError):
    """자식 추론 프로세스의 실패 단계·예외형·소요 시간을 보존하는 예외."""

    def __init__(
        self,
        error_code,
        *,
        error_type="RuntimeError",
        stage="process",
        elapsed_seconds=None,
    ):
        safe_code = _safe_error_code(error_code)
        super().__init__(safe_code)
        self.error_code = safe_code
        self.error_type = _safe_error_type(error_type)
        self.stage = _safe_stage(stage)
        self.elapsed_seconds = _safe_elapsed_seconds(elapsed_seconds)


class DetectionTimeoutError(TimeoutError):
    """자식 추론 프로세스의 시간 초과 단계·소요 시간을 보존하는 예외."""

    def __init__(
        self,
        error_code="DETECTION_MODEL_TIMEOUT",
        *,
        error_type="TimeoutError",
        stage="process",
        elapsed_seconds=None,
    ):
        safe_code = _safe_error_code(error_code, default="DETECTION_MODEL_TIMEOUT")
        super().__init__(safe_code)
        self.error_code = safe_code
        self.error_type = _safe_error_type(error_type, default="TimeoutError")
        self.stage = _safe_stage(stage)
        self.elapsed_seconds = _safe_elapsed_seconds(elapsed_seconds)


def _serve(connection, reference, arguments):
    startup_started = time.monotonic()
    try:
        tracker = load_factory(reference)(*arguments)
        if not all(
            callable(getattr(tracker, name, None)) for name in ("reset", "process")
        ):
            raise ValueError("Invalid detection processor")
        runtime = (
            tracker.runtime_metadata()
            if callable(getattr(tracker, "runtime_metadata", None))
            else None
        )
        connection.send({"kind": "ready", "runtime": runtime})
        while True:
            operation, frame = connection.recv()
            started = time.monotonic()
            try:
                if operation == "reset":
                    tracker.reset()
                    result = None
                elif operation == "process":
                    result = DetectionResult.model_validate(
                        tracker.process(frame)
                    ).model_dump(mode="json")
                    model_seconds = max(0.0, time.monotonic() - started)
                else:
                    raise ValueError("Invalid detection operation")
                response = {"kind": "result", "value": result}
                if operation == "process":
                    response["model_seconds"] = model_seconds
                connection.send(response)
            except Exception as exc:
                # 모델 예외의 원문에 경로·인증 정보를 포함하지 않고 예외형·단계·시간만 전달한다.
                connection.send(
                    {
                        "kind": "error",
                        "error_code": _classify_runtime_error(exc, operation),
                        "error_type": _safe_error_type(type(exc).__name__),
                        "stage": _safe_stage(operation),
                        "elapsed_seconds": max(0.0, time.monotonic() - started),
                    }
                )
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
            connection.send(
                {
                    "kind": "startup_error",
                    "error_code": error_code,
                    "error_type": _safe_error_type(type(exc).__name__),
                    "stage": "startup",
                    "elapsed_seconds": max(0.0, time.monotonic() - startup_started),
                }
            )
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
        self.last_error_details = None
        self._process = context.Process(
            target=_serve,
            args=(child, reference, (model_path, confidence, device)),
            daemon=True,
        )
        try:
            self._process.start()
            child.close()
            ready = self._exchange(startup_timeout_seconds, stage="startup")
            if ready.get("kind") != "ready":
                code = ready.get("error_code")
                if code not in _STARTUP_ERROR_CODES:
                    code = "DETECTION_STARTUP_FAILED"
                err = DetectionProcessError(
                    code,
                    error_type=ready.get("error_type") or "RuntimeError",
                    stage=ready.get("stage") or "startup",
                    elapsed_seconds=ready.get("elapsed_seconds"),
                )
                self._record_error(err)
                raise err
            self.runtime = ready.get("runtime")
        except BaseException as exc:
            if isinstance(exc, (DetectionProcessError, DetectionTimeoutError)):
                self._record_error(exc)
            child.close()
            self.close()
            raise

    def _record_error(self, exc):
        self.last_error_details = {
            "error_code": getattr(exc, "error_code", None),
            "error_type": getattr(exc, "error_type", type(exc).__name__),
            "stage": getattr(exc, "stage", None),
            "elapsed_seconds": getattr(exc, "elapsed_seconds", None),
        }

    def _exchange(self, timeout, message=None, *, stage="process"):
        # Pipe.send와 poll 이후 recv도 큰 프레임·불완전한 응답에서 블로킹할 수 있다.
        # I/O는 별도 스레드에 두어 전송부터 수신 완료까지 같은 제한으로 감독한다.
        started = time.monotonic()
        deadline = started + timeout
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
                err = DetectionProcessError(
                    "DETECTION_STOPPED",
                    error_type="RuntimeError",
                    stage=stage,
                    elapsed_seconds=0.0,
                )
                self._record_error(err)
                raise err
            self._io_thread = threading.Thread(
                target=exchange, name="detection-ipc", daemon=True
            )
            self._io_thread.start()
        while True:
            if self._closed or self._stop.is_set():
                err = DetectionProcessError(
                    "DETECTION_STOPPED",
                    error_type="RuntimeError",
                    stage=stage,
                    elapsed_seconds=max(0.0, time.monotonic() - started),
                )
                self._record_error(err)
                raise err
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                err = DetectionTimeoutError(
                    "DETECTION_MODEL_TIMEOUT",
                    error_type="TimeoutError",
                    stage=stage,
                    elapsed_seconds=max(0.0, time.monotonic() - started),
                )
                self._record_error(err)
                raise err
            if completed.wait(min(0.1, remaining)):
                if errors:
                    cause = errors[0]
                    err = DetectionProcessError(
                        "DETECTION_PROCESS_UNAVAILABLE",
                        error_type=type(cause).__name__,
                        stage=stage,
                        elapsed_seconds=max(0.0, time.monotonic() - started),
                    )
                    self._record_error(err)
                    raise err from None
                return result[0]

    def _request(self, operation, frame=None):
        with self._call_lock:
            try:
                if self._closed or self._stop.is_set():
                    err = DetectionProcessError(
                        "DETECTION_STOPPED",
                        error_type="RuntimeError",
                        stage=operation,
                        elapsed_seconds=0.0,
                    )
                    self._record_error(err)
                    raise err
                started = time.monotonic()
                result = self._exchange(
                    self.timeout_seconds, (operation, frame), stage=operation
                )
                if result.get("kind") != "result":
                    default_code = (
                        "DETECTION_RESET_FAILED"
                        if operation == "reset"
                        else "DETECTION_MODEL_FAILED"
                    )
                    elapsed = _safe_elapsed_seconds(result.get("elapsed_seconds"))
                    if elapsed is None:
                        elapsed = max(0.0, time.monotonic() - started)
                    err = DetectionProcessError(
                        _safe_error_code(result.get("error_code"), default=default_code),
                        error_type=_safe_error_type(
                            result.get("error_type"), default="RuntimeError"
                        ),
                        stage=_safe_stage(result.get("stage"), default=operation),
                        elapsed_seconds=elapsed,
                    )
                    self._record_error(err)
                    raise err
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
