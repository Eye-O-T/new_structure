"""포화된 outbox의 미등록 crop을 실제 Data 정리·객체 처리 경계에서 검증한다."""

from datetime import timedelta
import json
import os

import cv2
import httpx
import numpy as np
import pytest

from ai_cctv_core.processing.worker import ObjectWorker
from ai_cctv_core.time import format_utc, utc_now
from server.services.data.app.api.events import create_event
from server.services.data.app.config import Settings
from server.services.data.app.database.connection import Database
from server.services.data.app.database.repositories import DataRepository
from server.services.data.app.main import create_app
from server.services.data.app.schemas import EventCreate, RetentionRequest
from server.services.data.app.storage.protection import read_snapshot_protection
from server.services.data.app.storage.retention import retention_cleanup
from server.services.preprocessing.app.event_publisher import EventPublisher
from server.services.preprocessing.app import event_publisher
from server.services.preprocessing.processors.identity.appearance import (
    LocalAppearanceIdentity,
)


@pytest.mark.asyncio
async def test_old_crop_waiting_for_full_outbox_survives_gc_and_is_processed(tmp_path):
    settings = Settings(
        database_path=tmp_path / "db/data.db",
        storage_root=tmp_path / "recordings",
        snapshot_root=tmp_path / "snapshots",
        backup_root=tmp_path / "backups",
        internal_token="t" * 32,
    )
    settings.prepare_directories()
    repository = DataRepository(Database(settings.database_path))
    repository.initialize()
    repository.create_camera(
        {"camera_id": "cam-001", "name": "camera", "stream_path": "cam-001"}
    )

    class Data:
        def create_event(self, payload):
            return create_event(
                EventCreate.model_validate(payload), repository, settings
            )

    publisher = EventPublisher(
        Data(), settings.snapshot_root / "outbox.db", max_pending=1
    )
    publisher.submit(
        {
            "camera_id": "cam-001",
            "event_type": "person_detected",
            "occurred_at": format_utc(utc_now()),
            "metadata": {},
        }
    )
    crop = settings.snapshot_root / "waiting-crop.jpg"
    assert cv2.imwrite(str(crop), np.full((32, 16, 3), 128, dtype=np.uint8))
    old = utc_now() - timedelta(days=10)
    os.utime(crop, (old.timestamp(), old.timestamp()))
    waiting = {
        "camera_id": "cam-001",
        "source_event_id": "a" * 32,
        "event_type": "person_appeared",
        "person_id": "1",
        "occurred_at": format_utc(old),
        "object_observation": {
            "tracking_session_id": "c" * 32,
            "bbox": [0, 0, 16, 32],
            "frame_width": 16,
            "frame_height": 32,
            "crop_path": crop.name,
        },
    }
    try:
        with pytest.raises(RuntimeError, match="full"):
            publisher.submit(waiting)
        publisher.refresh_protection()
        assert crop.name in read_snapshot_protection(settings).paths
        result = retention_cleanup(
            repository, settings, RetentionRequest(retention_days=7, dry_run=False)
        )
        assert result["snapshots_deleted"] == 0
        assert crop.is_file()
        assert publisher.deliver_once()
        # 큐 공간이 생겨도 재등록 전까지 대기 이미지는 계속 보호한다.
        retention_cleanup(
            repository, settings, RetentionRequest(retention_days=7, dry_run=False)
        )
        assert crop.is_file()
        publisher.submit(waiting)
        assert publisher.deliver_once()
        app = create_app(settings=settings, repository=repository)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://data/internal/v1",
            headers={"X-Internal-Token": settings.internal_token},
        ) as client:
            worker = ObjectWorker(
                "identity", client, settings.snapshot_root, LocalAppearanceIdentity()
            )
            await worker.once()
        events = repository.search_events(
            camera_id="cam-001",
            event_type="person_appeared",
            start_time=None,
            end_time=None,
            limit=10,
            offset=0,
        )
        assert len(events) == 1
        assert events[0]["metadata"]["identity"]["status"] == "complete"
    finally:
        scanner = getattr(repository, "snapshot_scanner", None)
        if scanner is not None:
            scanner.close()


def test_over_capacity_protection_explicitly_defers_data_cleanup(tmp_path, monkeypatch):
    settings = Settings(
        database_path=tmp_path / "data.db",
        storage_root=tmp_path / "recordings",
        snapshot_root=tmp_path,
        backup_root=tmp_path / "backups",
        internal_token="t" * 32,
    )
    publisher = EventPublisher(None, tmp_path / "outbox.db", max_pending=1)
    publisher.submit({"camera_id": "cam-001", "source_event_id": "a" * 32})
    monkeypatch.setattr(event_publisher, "MAX_PROTECTED_EVENTS", 1)
    with pytest.raises(RuntimeError):
        publisher.submit({"camera_id": "cam-002", "source_event_id": "b" * 32})
    protection = read_snapshot_protection(settings)
    assert not protection.ready
    assert protection.reason == "OUTBOX_MANIFEST_INCOMPLETE"
    assert json.loads(publisher.protection_path.read_bytes())["schema_version"] == 2
    # v1만 아는 구형 Data도 v2를 거절해야 불완전한 빈 목록으로 삭제하지 않는다.


@pytest.mark.parametrize(
    "version,complete,ready", [(1, None, True), (2, None, False), (2, True, True)]
)
def test_manifest_upgrade_requires_explicit_completeness(
    tmp_path, version, complete, ready
):
    settings = Settings(
        database_path=tmp_path / "data.db",
        storage_root=tmp_path / "recordings",
        snapshot_root=tmp_path,
        backup_root=tmp_path / "backups",
        internal_token="t" * 32,
    )
    publisher = EventPublisher(None, tmp_path / "outbox.db")
    manifest = {
        "schema_version": version,
        "generated_at": format_utc(utc_now()),
        "paths": [],
        "events": [],
    }
    if complete is not None:
        manifest["complete"] = complete
    publisher.protection_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert read_snapshot_protection(settings).ready is ready
