"""한 복구 작업의 HTTP·파일 I/O를 종료 가능한 자식에서 수행한다."""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
import multiprocessing
import time

from ai_cctv_core.identifiers import safe_storage_path
from ai_cctv_core.time import parse_utc


def _serve(connection, job, settings):
    from .recovery import RecoveryCoordinator, RecoveryError

    def progress(stage):
        connection.send({"kind": "progress", "stage": stage})

    def temporary(path):
        connection.send(
            {
                "kind": "temporary",
                "path": path.relative_to(settings.storage_root.resolve()).as_posix()
                if path
                else None,
            }
        )

    try:
        if not job.get("recovery_url"):
            raise RecoveryError("RECOVERY_ORIGIN_UNAVAILABLE")
        start = parse_utc(job["outage_started_at"])
        end = parse_utc(job["outage_ended_at"])
        aggregate = {
            "camera_id": job["camera_id"],
            "selected": 0,
            "downloaded": 0,
            "reused": 0,
            "indexed": 0,
            "idempotent_replays": 0,
            "chunks": 0,
        }
        while start < end:
            chunk_end = min(start + timedelta(hours=24), end)
            coordinator = RecoveryCoordinator(
                edge_base_url=job["recovery_url"],
                camera_id=job["camera_id"],
                recovery_token=job["auth_token"],
                data_base_url=settings.recovery_data_base_url,
                internal_token=settings.data_api_tokens()["recovery"],
                recordings_root=settings.storage_root,
                timeout_seconds=settings.recovery_timeout_seconds,
                progress_callback=progress,
                temporary_callback=temporary,
            )
            summary = asdict(coordinator.recover(start, chunk_end))
            for key in (
                "selected",
                "downloaded",
                "reused",
                "indexed",
                "idempotent_replays",
            ):
                aggregate[key] += int(summary[key])
            aggregate["chunks"] += 1
            start = chunk_end
        connection.send({"kind": "complete", "summary": aggregate})
    except RecoveryError as exc:
        connection.send({"kind": "error", "code": str(exc)[:1024]})
    except Exception:
        connection.send({"kind": "error", "code": "RECOVERY_WORKER_ERROR"})
    finally:
        connection.close()


def run_recovery_job(job, settings, progress_callback, *, stop_event=None):
    from .recovery import RecoveryError, RecoveryProcessDidNotStop

    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_serve, args=(child, job, settings), daemon=True)
    deadline = time.monotonic() + settings.recovery_job_timeout_seconds
    temporary = None

    def temporary_path(message):
        relative = message["path"]
        return safe_storage_path(settings.storage_root, relative) if relative else None

    try:
        process.start()
        child.close()
        while True:
            if stop_event is not None and stop_event.is_set():
                raise RecoveryError("RECOVERY_INTERRUPTED")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RecoveryError("RECOVERY_JOB_TIMEOUT")
            try:
                if not parent.poll(min(0.05, remaining)):
                    if not process.is_alive():
                        raise RecoveryError("RECOVERY_PROCESS_EXITED")
                    continue
                message = parent.recv()
            except (EOFError, OSError) as exc:
                raise RecoveryError("RECOVERY_PROCESS_EXITED") from exc
            kind = message["kind"]
            if kind == "temporary":
                temporary = temporary_path(message)
            elif kind == "progress":
                progress_callback(message["stage"])
            elif kind == "complete":
                return message["summary"]
            elif kind == "error":
                raise RecoveryError(message["code"])
            else:
                raise RecoveryError("RECOVERY_PROCESS_INVALID")
    finally:
        child.close()
        if process.pid is not None:
            # 정상 반환도 부모가 자식 종료를 확인한 뒤 다음 작업을 시작한다.
            process.join(timeout=0.2)
            if process.is_alive():
                try:
                    process.terminate()
                except OSError:
                    pass
                process.join(timeout=1)
            if process.is_alive():
                try:
                    process.kill()
                except OSError:
                    pass
                process.join(timeout=1)
            if process.is_alive():
                parent.close()
                raise RecoveryProcessDidNotStop("RECOVERY_PROCESS_DID_NOT_STOP")
        # 기한·취소와 동시에 도착한 생성 통지도 자식 종료 후 모두 확인한다.
        # 자식이 작고 고정된 메시지만 쓰므로 EOF까지 읽어도 새 I/O를 기다리지 않는다.
        while True:
            try:
                if not parent.poll():
                    break
                message = parent.recv()
            except (EOFError, OSError):
                break
            if message.get("kind") == "temporary":
                temporary = temporary_path(message)
        parent.close()
        process.close()
        if temporary is not None:
            # 자식이 열었다고 알린 단일 .part만 정리한다. 완성·검증된 영상은 보존한다.
            if temporary.name.startswith(".recovery-") and temporary.suffix == ".part":
                temporary.unlink(missing_ok=True)
