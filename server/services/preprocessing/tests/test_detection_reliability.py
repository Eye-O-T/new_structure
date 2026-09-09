"""실제 카메라나 모델 없이 탐지 재시도·발송함·유한 종료 경계를 검증한다."""

import threading
import time
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import cv2
import httpx
import numpy as np
import pytest

from server.services.preprocessing.app import pipeline, supervisor
from server.services.preprocessing.app import objects as observation_files
from server.services.preprocessing.app.data_client import DataClient
from server.services.preprocessing.app.event_publisher import EventPublisher
from server.services.preprocessing.app.live_publisher import LivePublisher
from server.services.preprocessing.app.objects import (
    save_observation,
    write_jpeg_atomic,
)
from server.services.preprocessing.app.settings import Settings
from server.services.preprocessing.processors.detection.contracts import DetectionResult
from server.services.preprocessing.processors.detection.yolo import YoloTracker


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_service_url="http://data/internal/v1",
        internal_service_token="t" * 40,
        rtsp_base_url="rtsp://media:8554",
        media_read_username="reader",
        media_read_password="r" * 40,
        snapshots_root=tmp_path,
        model_path=tmp_path / "model.pt",
        device="cpu",
        confidence=0.4,
        analysis_fps=5,
        disappear_seconds=3,
        refresh_seconds=15,
        inference_enabled=True,
        model_retry_seconds=0.5,
    )


class Data:
    def __init__(self):
        self.events = []
        self.statuses = []
        self.closed = False

    def create_event(self, payload):
        self.events.append(payload)

    def set_camera_status(self, camera_id, status):
        self.statuses.append((camera_id, status))

    def close(self):
        self.closed = True


def event():
    return {
        "camera_id": "cam-001",
        "event_type": "person_appeared",
        "occurred_at": "2026-09-09T00:00:00Z",
        "person_id": "7",
        "metadata": {},
    }


def wait_until(condition):
    deadline = time.monotonic() + 3
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def test_shutdown_counts_all_selected_observations_when_crop_storage_fails(
    settings, monkeypatch
):
    worker = pipeline.CameraWorker({"camera_id": "cam-001"}, settings, Data())
    worker.stop_event.set()
    tracker = SimpleNamespace(
        process=lambda frame: DetectionResult(
            objects=[
                {"person_id": str(i), "bbox": [0, 0, 20, 20], "confidence": 0.9}
                for i in range(2)
            ]
        )
    )

    def fail(*args):
        raise OSError("full")

    monkeypatch.setattr(pipeline, "save_observation", fail)
    worker._process_frame(
        np.zeros((20, 20, 3), dtype=np.uint8),
        tracker,
        pipeline.TrackState(3),
        0,
        float("-inf"),
    )
    assert worker.status.event_shutdown_losses == 2


@pytest.mark.parametrize(
    "name,value",
    [
        ("analysis_fps", float("nan")),
        ("analysis_fps", float("inf")),
        ("refresh_seconds", 0),
        ("refresh_seconds", -1),
        ("disappear_seconds", float("nan")),
        ("capture_timeout_seconds", float("inf")),
        ("model_retry_seconds", 0),
        ("event_outbox_max_pending", 0),
        ("event_outbox_max_bytes", 0),
        ("data_service_url", "http://admin:password@data"),
        ("rtsp_base_url", "rtsp://media:99999"),
    ],
)
def test_settings_reject_unbounded_or_invalid_runtime_inputs(settings, name, value):
    with pytest.raises(ValueError):
        replace(settings, **{name: value}).validate()


@pytest.mark.parametrize(
    "path",
    ["", "../camera", "/camera", "cam/../other", "cam//other", "cam\\other", "cam\x00"],
)
def test_stream_paths_cannot_escape_selected_rtsp_base(settings, path):
    with pytest.raises(ValueError, match="relative path"):
        settings.rtsp_source_url(path)


def test_explicit_missing_configuration_and_bad_boolean_are_not_silently_ignored(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AI_CCTV_CONFIG_FILE", str(tmp_path / "absent.yaml"))
    with pytest.raises(FileNotFoundError):
        Settings.from_env()
    monkeypatch.delenv("AI_CCTV_CONFIG_FILE")
    monkeypatch.setenv("INFERENCE_ENABLED", "tru")
    with pytest.raises(ValueError, match="boolean"):
        Settings.from_env()


def test_identity_default_uses_osnet_processor(settings, monkeypatch):
    monkeypatch.delenv("AI_CCTV_CONFIG_FILE", raising=False)
    monkeypatch.delenv("IDENTITY_PLUGIN", raising=False)
    assert settings.identity_plugin.endswith(":OsNetIdentity")
    assert Settings.from_env().identity_plugin == settings.identity_plugin


def test_outbox_storage_error_remains_visible_when_status_cannot_read_database(
    tmp_path, monkeypatch
):
    publisher = EventPublisher(Data(), tmp_path / "outbox.sqlite3")
    with monkeypatch.context() as patch:

        def unavailable(*_, **__):
            raise sqlite3.OperationalError("storage unavailable")

        patch.setattr(sqlite3, "connect", unavailable)
        with pytest.raises(sqlite3.OperationalError):
            publisher.submit(event())
        assert publisher.status() == {
            "pending": None,
            "rejected": None,
            "waiting": None,
            "last_error": "EVENT_STORAGE",
        }
    publisher.submit(event())
    assert publisher.status() == {
        "pending": 1,
        "rejected": 0,
        "waiting": 0,
        "last_error": None,
    }


def test_outbox_survives_restart_and_reuses_source_id_after_lost_response(tmp_path):
    calls = []

    class Client:
        def create_event(self, payload):
            calls.append(payload)
            if len(calls) == 1:
                raise httpx.ReadTimeout("response lost after the server committed")

    path = tmp_path / "outbox.sqlite3"
    first = EventPublisher(Client(), path)
    first.submit(event())
    with pytest.raises(httpx.ReadTimeout):
        first.deliver_once()
    second = EventPublisher(Client(), path)
    assert second.status()["pending"] == 1
    assert second.deliver_once()
    assert second.status()["pending"] == 0
    assert calls[0] == calls[1]
    assert len(calls[0]["source_event_id"]) == 32
    assert "edge_event_id" not in calls[0]


def test_outbox_limits_retain_existing_records_and_permanent_rejection_does_not_block(
    tmp_path,
):
    calls = []

    class Client:
        def create_event(self, payload):
            calls.append(payload)
            if len(calls) == 1:
                request = httpx.Request("POST", "http://data/events")
                raise httpx.HTTPStatusError(
                    "invalid event",
                    request=request,
                    response=httpx.Response(422, request=request),
                )

    publisher = EventPublisher(Client(), tmp_path / "outbox.sqlite3", max_pending=2)
    publisher.submit(event())
    publisher.submit({**event(), "person_id": "8"})
    with pytest.raises(RuntimeError, match="full"):
        publisher.submit(event())
    assert publisher.status()["pending"] == 2
    assert publisher.deliver_once()
    assert publisher.deliver_once()
    assert publisher.status()["rejected"] == 1
    assert publisher.status()["pending"] == 0
    assert calls[1]["person_id"] == "8"
    bounded = EventPublisher(Data(), tmp_path / "small.sqlite3", max_bytes=1)
    with pytest.raises(RuntimeError, match="full"):
        bounded.submit(event())
    assert bounded.status()["pending"] == 0


def test_full_outbox_pauses_observation_creation_and_retries_the_same_event(
    settings, monkeypatch
):
    client = Data()
    publisher = EventPublisher(
        client, settings.snapshots_root / "outbox.sqlite3", max_pending=1
    )
    publisher.submit(event())
    worker = pipeline.CameraWorker(
        {"camera_id": "cam-001"}, settings, client, event_publisher=publisher
    )
    snapshots, submissions = [], []
    submit = publisher.submit

    def capture_submit(payload):
        submissions.append(dict(payload))
        submit(payload)

    def snapshot(*_):
        snapshots.append(len(snapshots) + 1)
        return f"snapshot-{snapshots[-1]}.jpg"

    monkeypatch.setattr(publisher, "submit", capture_submit)
    monkeypatch.setattr(worker, "_snapshot", snapshot)
    tracker = SimpleNamespace(
        process=lambda _: DetectionResult(
            objects=[
                {"person_id": "1", "confidence": 0.9, "bbox": [1, 1, 5, 5]},
                {"person_id": "2", "confidence": 0.9, "bbox": [5, 5, 10, 10]},
            ]
        )
    )
    thread = threading.Thread(
        target=worker._process_frame,
        args=(
            np.zeros((20, 20, 3), dtype=np.uint8),
            tracker,
            pipeline.TrackState(3),
            0,
            float("-inf"),
        ),
    )
    thread.start()
    try:
        wait_until(lambda: worker.status.event_backpressure)
        assert snapshots == [1]
        assert publisher.deliver_once()
        wait_until(lambda: len(snapshots) == 2 and worker.status.event_backpressure)
        assert publisher.deliver_once()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert publisher.deliver_once()
        person_one = [entry for entry in submissions if entry["person_id"] == "1"]
        assert len(person_one) >= 2 and all(
            entry == person_one[0] for entry in person_one
        )
        assert worker.status.event_backpressure is False
        assert worker.status.event_delivery_error is None
        assert worker.status.event_shutdown_losses == 0
        assert [item["person_id"] for item in client.events] == ["7", "1", "2"]
    finally:
        worker.stop()
        thread.join(timeout=1)


def test_outbox_backpressure_can_stop_without_unbounded_wait_or_silent_loss(settings):
    publisher = EventPublisher(
        Data(), settings.snapshots_root / "outbox.sqlite3", max_pending=1
    )
    publisher.submit(event())
    worker = pipeline.CameraWorker(
        {"camera_id": "cam-001"}, settings, Data(), event_publisher=publisher
    )
    thread = threading.Thread(target=worker._event, args=("person_appeared",))
    thread.start()
    try:
        wait_until(lambda: worker.status.event_backpressure)
        worker.stop()
        thread.join(timeout=0.5)
        assert not thread.is_alive()
        assert worker.status.event_shutdown_losses == 1
        assert worker.status.event_persistence_failures >= 1
        assert publisher.status()["pending"] == 1
    finally:
        worker.stop()
        thread.join(timeout=1)


def test_persistent_event_submission_does_not_wait_for_network(settings):
    entered, release = threading.Event(), threading.Event()

    class BlockingData(Data):
        def create_event(self, payload):
            entered.set()
            assert release.wait(3)
            super().create_event(payload)

    client = BlockingData()
    publisher = EventPublisher(client, settings.snapshots_root / "outbox.sqlite3")
    worker = pipeline.CameraWorker(
        {"camera_id": "cam-001"}, settings, client, event_publisher=publisher
    )
    publisher.start()
    try:
        worker._event("person_appeared", person_id="7")
        assert entered.wait(1)
        worker._event("person_appeared", person_id="8")
        assert publisher.status()["pending"] == 2
        started = time.monotonic()
        publisher.close(timeout=0.01)
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        publisher.close(timeout=1)


@pytest.mark.parametrize("failure", ["prepare", "process"])
def test_model_failure_is_retried_in_same_worker_and_new_session(
    settings, monkeypatch, failure
):
    clock = [0.0]
    attempts, sessions = [], []
    client = Data()

    class Tracker:
        def __init__(self, attempt):
            self.attempt = attempt

        def reset(self):
            pass

        def process(self, frame):
            sessions.append(frame.tracking_session_id)
            if failure == "process" and self.attempt == 1:
                raise RuntimeError("temporary model failure")
            return DetectionResult()

    def factory(*_):
        attempts.append(len(attempts) + 1)
        if failure == "prepare" and len(attempts) == 1:
            raise OSError("model was not ready")
        return Tracker(len(attempts))

    worker = pipeline.CameraWorker(
        {"camera_id": "cam-001"}, settings, client, tracker_factory=factory
    )
    captures = []

    class Capture:
        released = False

        def isOpened(self):
            return True

        def read(self):
            clock[0] += 1
            if clock[0] > 3:
                worker.stop()
                return False, None
            return True, np.zeros((20, 20, 3), dtype=np.uint8)

        def release(self):
            self.released = True

    def capture(*arguments):
        assert arguments[2] == [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
            5000,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC,
            5000,
        ]
        result = Capture()
        captures.append(result)
        return result

    monkeypatch.setattr(cv2, "VideoCapture", capture)
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: clock[0])
    worker._run()
    assert attempts == [1, 2]
    assert worker.status.model_ready is True
    assert worker.status.last_error is None
    assert len(captures) == 1 and captures[0].released
    if failure == "process":
        assert sessions[0] != sessions[1]


def test_capture_is_released_even_when_backend_read_raises(settings, monkeypatch):
    worker = pipeline.CameraWorker(
        {"camera_id": "cam-001"}, replace(settings, inference_enabled=False), Data()
    )
    capture = SimpleNamespace(
        isOpened=lambda: True, release=lambda: released.append(True)
    )
    released = []

    def read():
        worker.stop()
        raise OSError("video backend failure")

    capture.read = read
    monkeypatch.setattr(cv2, "VideoCapture", lambda *_: capture)
    worker._run()
    assert released == [True]
    assert worker.status.state == "offline"


def test_model_reset_failure_is_reported_and_reprepared(settings, monkeypatch):
    clock, attempts, errors = [0.0], [], []

    class Tracker:
        def __init__(self, attempt):
            self.attempt = attempt
            self.resets = 0

        def reset(self):
            self.resets += 1
            if self.attempt == 1 and self.resets == 2:
                raise RuntimeError("reset failed after the stream opened")

        def process(self, _frame):
            return DetectionResult()

    def factory(*_):
        attempts.append(len(attempts) + 1)
        return Tracker(attempts[-1])

    worker = pipeline.CameraWorker(
        {"camera_id": "cam-001"}, settings, Data(), tracker_factory=factory
    )

    def read():
        clock[0] += 1
        errors.append(worker.status.last_error)
        if clock[0] > 1:
            worker.stop()
        return True, np.zeros((20, 20, 3), dtype=np.uint8)

    capture = SimpleNamespace(isOpened=lambda: True, read=read, release=lambda: None)
    monkeypatch.setattr(cv2, "VideoCapture", lambda *_: capture)
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: clock[0])
    worker._run()
    assert attempts == [1, 2]
    assert errors[0] == "model reset failed: RuntimeError"
    assert worker.status.last_error is None and worker.status.model_ready


def test_stop_during_read_does_not_publish_or_process_late_frame(settings, monkeypatch):
    client = Data()
    processed, released = [], []
    tracker = SimpleNamespace(
        reset=lambda: None, process=lambda _: processed.append(True)
    )
    worker = pipeline.CameraWorker(
        {"camera_id": "cam-001"}, settings, client, tracker_factory=lambda *_: tracker
    )

    def read():
        worker.stop()
        return True, np.zeros((20, 20, 3), dtype=np.uint8)

    capture = SimpleNamespace(
        isOpened=lambda: True, read=read, release=lambda: released.append(True)
    )
    monkeypatch.setattr(cv2, "VideoCapture", lambda *_: capture)
    worker._run()
    assert not processed and not client.events and not client.statuses
    assert released == [True]


def test_supervisor_retains_stopping_worker_until_exit_and_ignores_late_list(
    settings, monkeypatch
):
    created = []

    class Worker:
        def __init__(self, camera, _settings, _client, **_kwargs):
            self.stream_path = camera.get("stream_path") or camera["camera_id"]
            self.status = SimpleNamespace(state="starting")
            self.alive = False
            self.stops = 0
            self.ident = None
            created.append(self)

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def stop(self):
            self.stops += 1

        def status_snapshot(self):
            return dict(vars(self.status))

    monkeypatch.setattr(supervisor, "CameraWorker", Worker)
    client = Data()
    manager = supervisor.DetectionSupervisor(settings, client)
    manager._reconcile([{"camera_id": "cam-001"}])
    manager.events.max_pending = 1
    manager.events.submit(event())
    with pytest.raises(RuntimeError, match="full"):
        manager.events.submit({**event(), "snapshot_path": "waiting.jpg"})
    manager._reconcile([])
    # 종료 요청만으로는 아직 살아 있는 생산자의 대기 참조를 해제하지 않는다.
    assert manager.events.status()["waiting"] == 1
    manager._reconcile([{"camera_id": "cam-001"}])
    assert len(created) == 1 and created[0].stops == 1
    manager._reconcile([{"camera_id": "cam-001", "stream_path": "updated"}])
    assert len(created) == 1 and created[0].stops == 2
    created[0].alive = False
    manager._reconcile([{"camera_id": "cam-001", "stream_path": "updated"}])
    assert len(created) == 2 and created[1].stream_path == "updated"
    state = manager.status()
    state["workers"]["cam-001"]["state"] = "changed"
    assert created[1].status.state == "starting"
    created[1].alive = False
    manager._reconcile([])
    assert manager.events.status()["waiting"] == 0
    assert manager.events.status()["pending"] == 1
    manager.stop()
    manager._reconcile([{"camera_id": "cam-002"}])
    assert len(created) == 2 and client.closed


@pytest.mark.parametrize("capture_timeout,threshold", [(5, 30), (30, 60)])
def test_frame_staleness_is_calculated_outside_a_blocked_camera_thread(
    settings, monkeypatch, capture_timeout, threshold
):
    settings = replace(settings, capture_timeout_seconds=capture_timeout)
    clock = [10.0]
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: clock[0])
    worker = pipeline.CameraWorker({"camera_id": "cam-001"}, settings, Data())
    assert worker.status_snapshot()["frame_age_seconds"] is None
    assert worker.status_snapshot()["frame_stale"] is False
    clock[0] = 10.0 + threshold
    assert worker.status_snapshot()["frame_stale"] is True
    worker._last_frame_monotonic = clock[0]
    worker.status.model_ready = True
    worker.status.state = "online"
    clock[0] += threshold - 0.1
    assert worker.status_snapshot()["frame_stale"] is False
    clock[0] = 10.0 + threshold * 2
    assert worker.status_snapshot()["frame_age_seconds"] == threshold
    assert worker.status_snapshot()["frame_stale"] is True
    manager = supervisor.DetectionSupervisor(settings, Data())
    manager._workers[worker.camera_id] = worker
    assert manager.status()["workers"][worker.camera_id]["frame_stale"] is True


def test_crop_storage_recovers_before_event_and_both_jobs_are_saved(
    settings, monkeypatch
):
    from fastapi.testclient import TestClient
    from server.services.data.app.config import Settings as DataSettings
    from server.services.data.app.main import create_app

    data_settings = DataSettings(
        database_path=settings.snapshots_root / "data.sqlite3",
        storage_root=settings.snapshots_root / "recordings",
        snapshot_root=settings.snapshots_root,
        backup_root=settings.snapshots_root / "backups",
        internal_token="",
        **{
            f"data_{name}_token": token * 40
            for name, token in {
                "external": "e",
                "inference": "t",
                "identity": "d",
                "analysis": "a",
                "media": "m",
                "recovery": "r",
            }.items()
        },
    )
    writes = []
    original_write = observation_files.write_jpeg_atomic

    def flaky_write(root, relative, frame):
        if relative.name.endswith("_crop.jpg"):
            writes.append(relative)
            if len(writes) == 1:
                raise OSError("temporary crop storage failure")
        original_write(root, relative, frame)

    monkeypatch.setattr(observation_files, "write_jpeg_atomic", flaky_write)
    app = create_app(data_settings)
    with TestClient(app) as client:
        repository = app.state.repository
        repository.create_camera(
            {"camera_id": "cam-001", "name": "test", "stream_path": "cam-001"}
        )

        class Adapter:
            def create_event(self, payload):
                response = client.post(
                    "/internal/v1/events",
                    json=payload,
                    headers={
                        "X-Internal-Token": settings.internal_service_token,
                    },
                )
                assert response.status_code == 201, response.text

        publisher = EventPublisher(
            Adapter(), settings.snapshots_root / "outbox.sqlite3"
        )
        worker = pipeline.CameraWorker(
            {"camera_id": "cam-001"}, settings, Data(), event_publisher=publisher
        )
        observed_at = datetime(2026, 9, 9, tzinfo=timezone.utc)
        tracker = SimpleNamespace(
            process=lambda _: DetectionResult(
                objects=[
                    {"person_id": "7", "confidence": 0.9, "bbox": [1, 1, 10, 10]},
                ]
            )
        )
        thread = threading.Thread(
            target=worker._process_frame,
            args=(
                np.zeros((20, 20, 3), dtype=np.uint8),
                tracker,
                pipeline.TrackState(3),
                0,
                float("-inf"),
                observed_at,
            ),
        )
        thread.start()
        try:
            wait_until(lambda: worker.status.observation_error is not None)
            assert worker.status.event_backpressure
            assert worker.status.event_delivery_error == "OBJECT_CROP_WRITE_FAILED"
            assert publisher.status()["pending"] == 0
            assert repository.claim_object_job("identity") is None
            assert repository.claim_object_job("analysis") is None
            thread.join(timeout=3)
            assert not thread.is_alive()
            assert len(writes) == 2
            assert publisher.deliver_once()
            identity = repository.claim_object_job("identity")
            analysis = repository.claim_object_job("analysis")
            assert identity is not None and analysis is not None
            assert identity["event_id"] == analysis["event_id"]
            assert (
                datetime.fromisoformat(
                    repository.get_event(identity["event_id"])["occurred_at"]
                )
                == observed_at
            )
            crop = settings.snapshots_root / identity["object_observation"]["crop_path"]
            assert cv2.imread(str(crop)).shape == (9, 9, 3)
            assert len(list(settings.snapshots_root.rglob("*.jpg"))) == 3
            assert worker.status.observation_error is None
            assert worker.status.observation_persistence_failures == 1
            assert worker.status.event_delivery_error is None
            assert not worker.status.event_backpressure
        finally:
            worker.stop()
            thread.join(timeout=1)


def test_crop_storage_wait_can_stop_without_registering_incomplete_appearance(
    settings, monkeypatch
):
    client = Data()
    worker = pipeline.CameraWorker({"camera_id": "cam-001"}, settings, client)
    snapshots = []

    def unavailable(*_):
        raise OSError("crop storage unavailable")

    monkeypatch.setattr(pipeline, "save_observation", unavailable)
    monkeypatch.setattr(worker, "_snapshot", lambda *_: snapshots.append(True))
    tracker = SimpleNamespace(
        process=lambda _: DetectionResult(
            objects=[
                {"person_id": "7", "confidence": 0.9, "bbox": [1, 1, 10, 10]},
            ]
        )
    )
    thread = threading.Thread(
        target=worker._process_frame,
        args=(
            np.zeros((20, 20, 3), dtype=np.uint8),
            tracker,
            pipeline.TrackState(3),
            0,
            float("-inf"),
        ),
    )
    thread.start()
    try:
        wait_until(lambda: worker.status.event_backpressure)
        worker.stop()
        thread.join(timeout=0.5)
        assert not thread.is_alive()
        assert not client.events
        assert snapshots == [True]
        assert worker.status.event_shutdown_losses == 1
        assert worker.status.observation_error == "OSError"
    finally:
        worker.stop()
        thread.join(timeout=1)


def test_malformed_camera_list_does_not_silently_remove_running_cameras():
    client = DataClient("http://data", "token")
    client._client.close()
    client._client = httpx.Client(
        base_url="http://data",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"unexpected": []})
        ),
    )
    try:
        with pytest.raises(ValueError, match="invalid enabled camera list"):
            client.enabled_cameras()
    finally:
        client.close()


def test_live_publisher_keeps_latest_frame_and_rejects_work_after_close():
    entered, release = threading.Event(), threading.Event()
    sent = []

    class Client:
        def put_live_objects(self, _camera, payload):
            sent.append(payload)
            if len(sent) == 1:
                entered.set()
                assert release.wait(3)

    publisher = LivePublisher(Client(), "cam-001")
    try:
        publisher.submit({"frame": 1})
        assert entered.wait(1)
        publisher.submit({"frame": 2})
        publisher.submit({"frame": 3})
        release.set()
        wait_until(lambda: len(sent) == 2)
        assert sent == [{"frame": 1}, {"frame": 3}]
    finally:
        release.set()
        publisher.close()
    publisher.submit({"frame": 4})
    assert publisher.pending.empty()


def test_observation_rejects_path_traversal_and_publishes_complete_jpeg(tmp_path):
    frame = np.zeros((20, 20, 3), dtype=np.uint8)
    detection = {"person_id": "1", "confidence": 0.9, "bbox": [1, 1, 10, 10]}
    with pytest.raises(ValueError, match="camera_id"):
        save_observation(frame, detection, tmp_path, "../escape", "a" * 32)
    observation = save_observation(frame, detection, tmp_path, "cam-001", "a" * 32)
    assert cv2.imread(str(tmp_path / observation["crop_path"])).shape == (9, 9, 3)
    assert not list(tmp_path.rglob(".snapshot-*"))


def test_snapshot_encoding_failure_never_leaves_partial_jpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(cv2, "imencode", lambda *_: (False, None))
    with pytest.raises(OSError, match="encode"):
        write_jpeg_atomic(
            tmp_path, Path("frame.jpg"), np.zeros((2, 2, 3), dtype=np.uint8)
        )
    assert not (tmp_path / "frame.jpg").exists()


def test_default_detector_requires_a_local_model_before_loading_ultralytics(tmp_path):
    with pytest.raises(ValueError, match="local file"):
        YoloTracker(tmp_path / "missing.pt", 0.4, "cpu")
    tracker = object.__new__(YoloTracker)
    tracker._model = SimpleNamespace()
    tracker.reset()
