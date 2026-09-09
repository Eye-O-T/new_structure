"""미전송·거부 crop 보호와 실패 시 이벤트 보존의 실제 SQLite 경계를 검증한다."""

import json
import sqlite3

import httpx
import pytest

from server.services.preprocessing.app.event_publisher import EventPublisher
from server.services.preprocessing.app.outbox_admin import main
from server.services.preprocessing.app import event_publisher


class Data:
    reject = False

    def create_event(self, event):
        if self.reject:
            response = httpx.Response(
                422, request=httpx.Request("POST", "http://data/events")
            )
            response.raise_for_status()


def payload():
    return {
        "camera_id": "cam-001",
        "source_event_id": "b" * 32,
        "event_type": "person_appeared",
        "snapshot_path": "cam-001/snapshot.jpg",
        "object_observation": {
            "crop_path": "cam-001/crop.jpg",
            "annotated_snapshot_path": None,
        },
    }


def test_manifest_protects_until_ack_and_keeps_rejected_entries(tmp_path):
    data = Data()
    publisher = EventPublisher(data, tmp_path / ".event-outbox.sqlite3")
    publisher.submit(payload())
    value = json.loads(publisher.protection_path.read_text(encoding="utf-8"))
    assert set(value["paths"]) == {"cam-001/snapshot.jpg", "cam-001/crop.jpg"}
    assert value["events"] == [{"camera_id": "cam-001", "source_event_id": "b" * 32}]
    data.reject = True
    assert publisher.deliver_once()
    publisher.refresh_protection()
    assert json.loads(publisher.protection_path.read_text())["paths"]
    assert main(["retry", "--root", str(tmp_path), "--sequence", "1"]) == 0
    data.reject = False
    assert publisher.deliver_once()
    assert json.loads(publisher.protection_path.read_text())["paths"] == []


def test_manifest_write_failure_rolls_back_submit_but_keeps_ack_protected(
    tmp_path, monkeypatch
):
    publisher = EventPublisher(Data(), tmp_path / ".event-outbox.sqlite3")

    def fail(_, **_kwargs):
        raise OSError("disk unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(publisher, "_write_protection", fail)
        with pytest.raises(OSError):
            publisher.submit(payload())
        assert publisher.status()["pending"] == 0
    publisher.submit(payload())
    with monkeypatch.context() as patch:
        patch.setattr(publisher, "_write_protection", fail)
        with pytest.raises(OSError):
            publisher.deliver_once()
        assert publisher.status()["pending"] == 0
        assert json.loads(publisher.protection_path.read_text())["paths"]
    publisher.refresh_protection()
    assert json.loads(publisher.protection_path.read_text())["paths"] == []


def test_ack_commit_failure_keeps_persisted_event_and_its_protection(
    tmp_path, monkeypatch
):
    publisher = EventPublisher(Data(), tmp_path / ".event-outbox.sqlite3")
    publisher.submit(payload())
    connect = sqlite3.connect

    class FailedDeleteCommit(sqlite3.Connection):
        deleting = False

        def execute(self, sql, *args):
            if sql.startswith("DELETE FROM pending_events"):
                self.deleting = True
            return super().execute(sql, *args)

        def __exit__(self, exc_type, exc_value, traceback):
            if self.deleting and exc_type is None:
                self.rollback()
                raise sqlite3.OperationalError("commit failed")
            return super().__exit__(exc_type, exc_value, traceback)

    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *a, **kw: connect(*a, factory=FailedDeleteCommit, **kw),
    )
    with pytest.raises(sqlite3.OperationalError):
        publisher.deliver_once()
    assert publisher.status()["pending"] == 1
    manifest = json.loads(publisher.protection_path.read_text())
    assert manifest["paths"]
    assert manifest["events"] == [{"camera_id": "cam-001", "source_event_id": "b" * 32}]


def test_prune_commit_failure_keeps_waiting_reference_and_manifest(
    tmp_path, monkeypatch
):
    publisher = EventPublisher(
        Data(), tmp_path / ".event-outbox.sqlite3", max_pending=1
    )
    publisher.submit(payload())
    with pytest.raises(RuntimeError):
        publisher.submit({**payload(), "camera_id": "cam-002"})
    before = publisher.protection_path.read_bytes()
    connect = sqlite3.connect

    class FailedPruneCommit(sqlite3.Connection):
        pruning = False

        def execute(self, sql, *args):
            if sql.startswith("DELETE FROM waiting_observations"):
                self.pruning = True
            return super().execute(sql, *args)

        def __exit__(self, exc_type, exc_value, traceback):
            if self.pruning and exc_type is None:
                self.rollback()
                raise sqlite3.OperationalError("commit failed")
            return super().__exit__(exc_type, exc_value, traceback)

    with monkeypatch.context() as patch:
        patch.setattr(
            sqlite3,
            "connect",
            lambda *a, **kw: connect(*a, factory=FailedPruneCommit, **kw),
        )
        with pytest.raises(sqlite3.OperationalError):
            publisher.prune_waiting(set())
    assert publisher.status()["waiting"] == 1
    assert publisher.protection_path.read_bytes() == before
    publisher.prune_waiting(set())
    assert publisher.status()["waiting"] == 0
    assert publisher.status()["pending"] == 1


def test_discard_requires_explicit_confirmation_and_only_removes_rejected(tmp_path):
    data = Data()
    data.reject = True
    publisher = EventPublisher(data, tmp_path / ".event-outbox.sqlite3")
    publisher.submit(payload())
    publisher.deliver_once()
    with pytest.raises(SystemExit):
        main(["discard", "--root", str(tmp_path), "--sequence", "1"])
    assert publisher.status()["rejected"] == 1
    main(["discard", "--root", str(tmp_path), "--sequence", "1", "--confirm-discard"])
    assert publisher.status()["rejected"] == 0


def test_manifest_capacity_rolls_back_submit_without_losing_existing(
    tmp_path, monkeypatch
):
    publisher = EventPublisher(Data(), tmp_path / ".event-outbox.sqlite3")
    publisher.submit(payload())
    monkeypatch.setattr(event_publisher, "MAX_PROTECTED_EVENTS", 1)
    with pytest.raises(RuntimeError, match="capacity"):
        publisher.submit({**payload(), "source_event_id": "a" * 32})
    assert publisher.status() == {
        "pending": 1,
        "rejected": 0,
        "waiting": 1,
        "last_error": "EVENT_OUTBOX_FULL",
    }
    # 현재 RAM 후보까지 합친 보호 목록이 한도를 넘으면 누락된 정상 목록 대신
    # 명시적으로 불완전하다고 게시해 Data 정리를 중단한다.
    manifest = json.loads(publisher.protection_path.read_bytes())
    assert manifest["complete"] is False
    assert manifest["paths"] == []
    assert publisher.deliver_once()
    assert json.loads(publisher.protection_path.read_bytes())["complete"] is True


def test_full_outbox_protection_survives_ack_other_publisher_and_restart(tmp_path):
    publisher = EventPublisher(
        Data(), tmp_path / ".event-outbox.sqlite3", max_pending=1
    )
    publisher.submit(payload())
    waiting = {
        **payload(),
        "source_event_id": "c" * 32,
        "snapshot_path": "cam-001/waiting.jpg",
        "object_observation": {"crop_path": "cam-001/waiting-crop.jpg"},
    }
    for _ in range(3):
        with pytest.raises(RuntimeError, match="full"):
            publisher.submit(waiting)
    assert publisher.status()["waiting"] == 1
    assert publisher.deliver_once()
    # 관리 CLI나 새 인스턴스의 heartbeat도 영속 대기 참조를 빠뜨리지 않는다.
    reopened = EventPublisher(Data(), publisher.path, max_pending=1)
    reopened.refresh_protection()
    manifest = json.loads(reopened.protection_path.read_bytes())
    assert manifest["complete"] is True
    assert set(manifest["paths"]) == {"cam-001/waiting.jpg", "cam-001/waiting-crop.jpg"}
    assert manifest["events"] == [{"camera_id": "cam-001", "source_event_id": "c" * 32}]
    publisher.submit(waiting)
    assert publisher.status()["waiting"] == 0
    assert publisher.deliver_once()
    assert json.loads(publisher.protection_path.read_bytes())["paths"] == []


def test_removed_camera_waiting_reference_is_released_without_dropping_queued_events(
    tmp_path,
):
    publisher = EventPublisher(
        Data(), tmp_path / ".event-outbox.sqlite3", max_pending=1
    )
    publisher.submit(payload())
    for camera in ("cam-001", "cam-002"):
        with pytest.raises(RuntimeError, match="full"):
            publisher.submit(
                {
                    **payload(),
                    "camera_id": camera,
                    "source_event_id": "d" * 32,
                    "snapshot_path": f"{camera}/waiting.jpg",
                    "object_observation": None,
                }
            )
    publisher.prune_waiting({"cam-001"})
    manifest = json.loads(publisher.protection_path.read_bytes())
    assert "cam-001/waiting.jpg" in manifest["paths"]
    assert "cam-002/waiting.jpg" not in manifest["paths"]
    assert publisher.status()["pending"] == 1
    assert publisher.status()["waiting"] == 1
    publisher.prune_waiting(set())
    assert publisher.status()["waiting"] == 0
    assert publisher.status()["pending"] == 1
    assert (
        "cam-001/crop.jpg"
        in json.loads(publisher.protection_path.read_bytes())["paths"]
    )
