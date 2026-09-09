"""모델을 별도 프로세스에 격리해 멈춘 추론을 종료한 뒤 다음 작업에서 다시 준비한다."""

import json
import multiprocessing
import threading
import time

from .plugins import load_factory


def _model_process(connection, reference):
    """자식은 이미지 읽기·모델 실행만 맡고 작업 임대와 완료 보고는 부모가 담당한다."""
    try:
        plugin = load_factory(reference)()
        if not callable(getattr(plugin, "process", None)):
            raise ValueError("Invalid processor")
        connection.send({"kind": "ready"})
        while True:
            request = connection.recv()
            if request is None:
                break
            job, crop_path = request
            try:
                result = plugin.process(job, crop_path)
                encoded = json.dumps(result, allow_nan=False)
                if not isinstance(result, dict) or len(encoded.encode()) > 256 * 1024:
                    raise ValueError("Invalid processor result")
                response = {"kind": "result", "value": result}
            except (ValueError, TypeError, FileNotFoundError):
                response = {"kind": "invalid"}
            except Exception:
                # 모델 예외에는 파일 경로·자격 증명이 있을 수 있어 원문을 전달하지 않는다.
                response = {"kind": "error"}
            connection.send(response)
    except (EOFError, BrokenPipeError):
        pass
    except Exception:
        try:
            connection.send({"kind": "startup_error"})
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        connection.close()


class IsolatedProcessor:
    """한 번에 작업 하나만 실행한다. 시간 초과한 자식이 종료돼야 새 자식을 허용한다."""

    def __init__(
        self,
        reference,
        *,
        timeout_seconds=120,
        startup_timeout_seconds=30,
        stop_event=None,
    ):
        self.reference = reference
        self.timeout_seconds = timeout_seconds
        self.startup_timeout_seconds = startup_timeout_seconds
        self.restartable = True
        self._process = None
        self._connection = None
        self._closed = stop_event if stop_event is not None else threading.Event()
        self._lock = threading.Lock()
        self._lifecycle = threading.RLock()
        self._start(startup_timeout_seconds)

    def _abort(self):
        # 종료 요청과 시작/오류 복구가 같은 Process 핸들을 동시에 닫지 않게 한다.
        with self._lifecycle:
            self._abort_locked()

    def _abort_locked(self):
        process, connection = self._process, self._connection
        self._process = None
        self._connection = None
        if connection is not None:
            connection.close()
        if process is not None:
            if process.pid is None:
                process.close()
                return
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            if process.is_alive():
                self.restartable = False
                self._process = process
                raise RuntimeError("MODEL_PROCESS_DID_NOT_STOP")
            process.close()

    def _start(self, timeout):
        with self._lifecycle:
            if self._closed.is_set() or not self.restartable:
                raise RuntimeError("MODEL_PROCESS_CLOSED")
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe()
            process = context.Process(
                target=_model_process, args=(child, self.reference), daemon=True
            )
            self._process, self._connection = process, parent
            try:
                process.start()
            except BaseException:
                child.close()
                self._abort_locked()
                raise
            child.close()
        try:
            deadline = time.monotonic() + timeout
            while True:
                if self._closed.is_set():
                    raise RuntimeError("MODEL_PROCESS_CLOSED")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("MODEL_STARTUP_TIMEOUT")
                if parent.poll(min(0.1, remaining)):
                    break
            if parent.recv().get("kind") != "ready":
                raise RuntimeError("MODEL_STARTUP_FAILED")
            if self._closed.is_set():
                raise RuntimeError("MODEL_PROCESS_CLOSED")
        except BaseException:
            self._abort()
            raise

    def process(self, job, crop_path):
        # 모델 재준비도 같은 기한에 포함해 재시도 중 Data의 임대 시간을 넘지 않게 한다.
        with self._lock:
            deadline = time.monotonic() + self.timeout_seconds
            if self._closed.is_set():
                raise RuntimeError("MODEL_PROCESS_CLOSED")
            try:
                if self._process is None:
                    self._start(min(self.startup_timeout_seconds, self.timeout_seconds))
                connection = self._connection
                connection.send((job, crop_path))
                if not connection.poll(max(0, deadline - time.monotonic())):
                    raise TimeoutError("MODEL_TIMEOUT")
                response = connection.recv()
            except BaseException:
                self._abort()
                raise
            if response.get("kind") == "invalid":
                raise ValueError("INVALID_OBJECT_RESULT_OR_CROP")
            if response.get("kind") != "result":
                self._abort()
                raise RuntimeError("MODEL_PROCESS_FAILED")
            return response["value"]

    def close(self):
        # process()가 응답을 기다릴 때도 종료할 수 있도록 실행 잠금은 얻지 않는다.
        self._closed.set()
        self._abort()
