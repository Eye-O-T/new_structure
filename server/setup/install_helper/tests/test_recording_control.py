import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts=false")

from PyQt5.QtGui import QImage
from PyQt5.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[4]
for candidate in (ROOT, ROOT / "lib"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))


def _ensure_service_stubs():
    if "httpx" not in sys.modules:
        try:
            import httpx  # noqa: F401
        except ModuleNotFoundError:
            httpx_mod = types.ModuleType("httpx")

            class RequestError(Exception):
                pass

            class AsyncBaseTransport:
                pass

            class Request:
                def __init__(self, method: str, url: str, json_body=None):
                    self.method = method
                    self.url = SimpleNamespace(path=urlsplit(url).path)
                    self.content = (
                        json.dumps(json_body).encode("utf-8")
                        if json_body is not None
                        else b""
                    )

            class Response:
                def __init__(self, status_code: int, *, json=None):
                    self.status_code = status_code
                    self._json = json

                def json(self):
                    if self._json is None:
                        raise ValueError("No JSON content")
                    return self._json

            class MockTransport(AsyncBaseTransport):
                def __init__(self, handler):
                    self.handler = handler

            class AsyncClient:
                def __init__(
                    self,
                    base_url: str = "",
                    timeout: float = 5.0,
                    transport=None,
                    trust_env: bool = False,
                ):
                    self.base_url = base_url
                    self.timeout = timeout
                    self.transport = transport

                async def request(self, method: str, path: str, json=None):
                    if self.transport is None:
                        raise RequestError("No transport configured")
                    full_url = urljoin(self.base_url, path)
                    return self.transport.handler(Request(method, full_url, json))

                async def aclose(self):
                    return None

            httpx_mod.RequestError = RequestError
            httpx_mod.AsyncBaseTransport = AsyncBaseTransport
            httpx_mod.Request = Request
            httpx_mod.Response = Response
            httpx_mod.MockTransport = MockTransport
            httpx_mod.AsyncClient = AsyncClient
            sys.modules["httpx"] = httpx_mod

    if "fastapi" not in sys.modules:
        try:
            import fastapi  # noqa: F401
        except ModuleNotFoundError:
            fastapi_mod = types.ModuleType("fastapi")

            class HTTPException(Exception):
                def __init__(self, status_code: int, detail: str = "", headers=None):
                    super().__init__(detail)
                    self.status_code = status_code
                    self.detail = detail
                    self.headers = headers or {}

            class Header:
                def __init__(self, default=None, **_kwargs):
                    self.default = default

            class APIRouter:
                def __init__(self, *_args, **_kwargs):
                    pass

                def include_router(self, *_args, **_kwargs):
                    return None

                def _decorator(self, *_args, **_kwargs):
                    def wrap(func):
                        return func

                    return wrap

                get = post = patch = put = delete = _decorator

            class FastAPI:
                def __init__(self, *_args, **_kwargs):
                    self.state = SimpleNamespace()
                    self.dependency_overrides = {}

                def include_router(self, *_args, **_kwargs):
                    return None

                def add_exception_handler(self, *_args, **_kwargs):
                    return None

                def exception_handler(self, *_args, **_kwargs):
                    def wrap(func):
                        return func

                    return wrap

            def Depends(dep=None):
                return dep

            def Query(default=None, **_kwargs):
                return default

            def Form(default=None, **_kwargs):
                return default

            def File(default=None, **_kwargs):
                return default

            def PathParam(default=None, **_kwargs):
                return default

            def Body(default=None, **_kwargs):
                return default

            def Security(dep=None, **_kwargs):
                return dep

            fastapi_mod.APIRouter = APIRouter
            fastapi_mod.Body = Body
            fastapi_mod.Depends = Depends
            fastapi_mod.FastAPI = FastAPI
            fastapi_mod.File = File
            fastapi_mod.Form = Form
            fastapi_mod.Header = Header
            fastapi_mod.HTTPException = HTTPException
            fastapi_mod.Path = PathParam
            fastapi_mod.Query = Query
            fastapi_mod.Request = object
            fastapi_mod.Response = object
            fastapi_mod.Security = Security
            fastapi_mod.UploadFile = object
            fastapi_mod.status = SimpleNamespace(
                HTTP_200_OK=200,
                HTTP_201_CREATED=201,
                HTTP_202_ACCEPTED=202,
                HTTP_204_NO_CONTENT=204,
                HTTP_400_BAD_REQUEST=400,
                HTTP_401_UNAUTHORIZED=401,
                HTTP_403_FORBIDDEN=403,
                HTTP_404_NOT_FOUND=404,
                HTTP_409_CONFLICT=409,
                HTTP_422_UNPROCESSABLE_ENTITY=422,
                HTTP_429_TOO_MANY_REQUESTS=429,
                HTTP_500_INTERNAL_SERVER_ERROR=500,
                HTTP_502_BAD_GATEWAY=502,
                HTTP_503_SERVICE_UNAVAILABLE=503,
            )
            sys.modules["fastapi"] = fastapi_mod

            exceptions_mod = types.ModuleType("fastapi.exceptions")
            exceptions_mod.RequestValidationError = Exception
            sys.modules["fastapi.exceptions"] = exceptions_mod

            security_mod = types.ModuleType("fastapi.security")
            security_mod.HTTPAuthorizationCredentials = object
            security_mod.HTTPBearer = lambda **_kwargs: None
            sys.modules["fastapi.security"] = security_mod

            responses_mod = types.ModuleType("fastapi.responses")
            responses_mod.FileResponse = object
            responses_mod.JSONResponse = object
            responses_mod.StreamingResponse = object
            sys.modules["fastapi.responses"] = responses_mod

    if "jwt" not in sys.modules:
        try:
            import jwt  # noqa: F401
        except ModuleNotFoundError:
            jwt_mod = types.ModuleType("jwt")

            class InvalidTokenError(Exception):
                pass

            class ExpiredSignatureError(InvalidTokenError):
                pass

            jwt_mod.InvalidTokenError = InvalidTokenError
            jwt_mod.ExpiredSignatureError = ExpiredSignatureError
            jwt_mod.encode = lambda *_args, **_kwargs: ""
            jwt_mod.decode = lambda *_args, **_kwargs: {}
            sys.modules["jwt"] = jwt_mod


_ensure_service_stubs()

import httpx  # noqa: E402
from server.desktop.admin.api import DesktopApi  # noqa: E402
from server.desktop.admin.application import CCTVMainWindow  # noqa: E402
from server.desktop.admin.settings_window import SettingsWindow  # noqa: E402
from server.services.data.app.config import Settings as DataSettings  # noqa: E402
from server.services.data.app.database.connection import Database  # noqa: E402
from server.services.data.app.database.repositories import DataRepository  # noqa: E402
from server.services.data.app.storage.recordings import (  # noqa: E402
    has_active_recording_file,
)
from server.services.external.app.api.cameras import (  # noqa: E402
    _apply_bulk_camera_recording,
    _apply_camera_recording,
    list_cameras_recording,
    sync_camera_recording_paths,
)
from server.services.external.app.clients.mediamtx import MediaMtxClient  # noqa: E402


class _SimulatedMediaMtxRuntime:
    """In-memory MediaMTX v1.9 control API + segment file writer simulation."""

    def __init__(self, storage_root: Path):
        self.storage_root = storage_root
        self.path_configs: dict[str, dict] = {}
        self.active_streams: dict[str, bool] = {}
        self.fail_cameras: set[str] = set()

    def set_stream_active(self, camera_id: str, active: bool) -> None:
        self.active_streams[camera_id] = active
        self._sync_recording_file(camera_id)

    def _sync_recording_file(self, camera_id: str) -> None:
        cfg = self.path_configs.get(camera_id, {})
        record_on = bool(cfg.get("record", False))
        stream_on = bool(self.active_streams.get(camera_id, False))
        seg_dir = self.storage_root / camera_id / "2026" / "10" / "27"
        seg_file = seg_dir / "20261027T103000-000000Z.mp4"
        if record_on and stream_on:
            seg_dir.mkdir(parents=True, exist_ok=True)
            seg_file.write_bytes(b"\x00\x00\x00\x18ftypmp42simulated-fmp4-stream")
        elif seg_file.exists() and not record_on:
            seg_file.unlink()

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path.lstrip("/")
            method = request.method.upper()

            if path == "v3/paths/list" and method == "GET":
                names = sorted(set(self.path_configs) | set(self.active_streams))
                items = []
                for name in names:
                    ready = bool(self.active_streams.get(name, False))
                    items.append(
                        {
                            "name": name,
                            "confName": name,
                            "ready": ready,
                            "source": (
                                {"type": "rtspSession", "id": f"sess-{name}"}
                                if ready
                                else None
                            ),
                        }
                    )
                return httpx.Response(200, json={"items": items})

            if path.startswith("v3/paths/get/") and method == "GET":
                camera_id = path.removeprefix("v3/paths/get/")
                if camera_id in self.fail_cameras:
                    return httpx.Response(503, json={"error": "simulated failure"})
                if (
                    camera_id not in self.path_configs
                    and camera_id not in self.active_streams
                ):
                    return httpx.Response(404)
                ready = bool(self.active_streams.get(camera_id, False))
                return httpx.Response(
                    200,
                    json={
                        "name": camera_id,
                        "confName": camera_id,
                        "ready": ready,
                        "source": (
                            {"type": "rtspSession", "id": f"sess-{camera_id}"}
                            if ready
                            else {"type": "publisher", "id": None}
                        ),
                    },
                )

            if path.startswith("v3/config/paths/get/") and method == "GET":
                camera_id = path.removeprefix("v3/config/paths/get/")
                if camera_id not in self.path_configs:
                    return httpx.Response(404)
                return httpx.Response(200, json=self.path_configs[camera_id])

            if path.startswith("v3/config/paths/patch/") and method == "PATCH":
                camera_id = path.removeprefix("v3/config/paths/patch/")
                if camera_id in self.fail_cameras:
                    return httpx.Response(503, json={"error": "simulated failure"})
                if camera_id not in self.path_configs:
                    return httpx.Response(404)
                body = json.loads(request.content.decode("utf-8"))
                self.path_configs[camera_id].update(body)
                self._sync_recording_file(camera_id)
                return httpx.Response(200)

            if path.startswith("v3/config/paths/add/") and method == "POST":
                camera_id = path.removeprefix("v3/config/paths/add/")
                if camera_id in self.fail_cameras:
                    return httpx.Response(503, json={"error": "simulated failure"})
                body = json.loads(request.content.decode("utf-8"))
                self.path_configs[camera_id] = dict(body)
                self._sync_recording_file(camera_id)
                return httpx.Response(200)

            if path.startswith("v3/config/paths/delete/") and method == "DELETE":
                camera_id = path.removeprefix("v3/config/paths/delete/")
                self.path_configs.pop(camera_id, None)
                return httpx.Response(200)

            return httpx.Response(404)

        return httpx.MockTransport(handler)


class _DirectDataAdapter:
    """Bridges External async DataClient calls to a real SQLite DataRepository."""

    def __init__(self, repository: DataRepository, settings: DataSettings):
        self.repository = repository
        self.settings = settings

    def _enrich(self, camera: dict | None) -> dict | None:
        if camera is None:
            return None
        result = dict(camera)
        result["recording_file_active"] = has_active_recording_file(
            str(result["camera_id"]), self.settings
        )
        return result

    async def get_camera(self, camera_id: str, *, user_id: str) -> dict:
        cam = self._enrich(self.repository.get_camera(camera_id))
        if cam is None:
            raise KeyError(camera_id)
        return cam

    async def list_cameras(
        self,
        *,
        user_id: str | None,
        limit: int,
        offset: int,
    ) -> dict:
        items = [
            self._enrich(item)
            for item in self.repository.list_cameras(limit, offset, user_id=user_id)
        ]
        return {"items": items, "limit": limit, "offset": offset}

    async def update_camera(self, camera_id: str, payload: dict) -> dict:
        updated = self.repository.update_camera(camera_id, payload)
        return self._enrich(updated) or {}


def _run_async(coro):
    try:
        while True:
            coro.send(None)
    except StopIteration as stop:
        return stop.value


class RecordingControlBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "cctv.db"
        self.storage_root = self.root / "recordings"
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.data_settings = DataSettings(
            database_path=self.db_path,
            storage_root=self.storage_root,
            snapshot_root=self.root / "snapshots",
            backup_root=self.root / "backups",
            internal_token="t" * 32,
        )
        self.mediamtx = _SimulatedMediaMtxRuntime(self.storage_root)
        self.external_settings = SimpleNamespace(
            media_control_url="http://mediamtx.internal",
            media_control_timeout_seconds=2.0,
        )
        self.locks: dict[str, asyncio.Lock] = {}

        def lock_factory(camera_id: str) -> asyncio.Lock:
            return self.locks.setdefault(camera_id, asyncio.Lock())

        self.request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    camera_lifecycle_lock_factory=lock_factory,
                    camera_admission_lock_factory=lock_factory,
                    media_client_factory=lambda: MediaMtxClient(
                        "http://mediamtx.internal",
                        verification_interval_seconds=0.0,
                        transport=self.mediamtx.transport(),
                    ),
                )
            )
        )
        self.principal = SimpleNamespace(user_id=1, role="admin")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_step1_migration_preserves_existing_cameras_and_defaults_new_cameras_to_stopped(
        self,
    ):
        migrations_dir = (
            ROOT
            / "server"
            / "services"
            / "data"
            / "app"
            / "database"
            / "migrations"
        )
        pre_011 = sorted(migrations_dir.glob("*.sql"))
        pre_011 = [p for p in pre_011 if not p.name.startswith("011_")]

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL);"
            )
            for path in pre_011:
                conn.executescript(path.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, '2026-10-27T00:00:00Z')",
                    (path.name,),
                )
            conn.execute(
                """
                INSERT INTO cameras(camera_id, name, stream_path, enabled, status, created_at, updated_at)
                VALUES ('existing-cam', '기존 카메라', 'existing-cam', 1, 'offline', '2026-10-27T00:00:00Z', '2026-10-27T00:00:00Z')
                """
            )
            conn.commit()
        finally:
            conn.close()

        repo = DataRepository(Database(self.db_path))
        repo.initialize()

        existing = repo.get_camera("existing-cam")
        self.assertIsNotNone(existing)
        self.assertTrue(existing["recording_enabled"])

        new_cam = repo.create_camera(
            {
                "camera_id": "new-cam",
                "name": "신규 카메라",
                "stream_path": "new-cam",
                "enabled": True,
            }
        )
        self.assertFalse(new_cam["recording_enabled"])

    def test_step2_and_step5_local_and_other_camera_recording_lifecycle_partial_failure_and_restart(
        self,
    ):
        repo = DataRepository(Database(self.db_path))
        repo.initialize()
        admin = repo.create_user(
            {"username": "admin", "password_hash": "hash", "role": "admin"}
        )
        self.principal = SimpleNamespace(user_id=admin["id"], role="admin")
        repo.create_camera(
            {
                "camera_id": "local-camera",
                "name": "노트북 웹캠",
                "stream_path": "local-camera",
                "enabled": True,
                "recording_enabled": False,
            }
        )
        repo.create_camera(
            {
                "camera_id": "cam-1",
                "name": "정문 카메라",
                "stream_path": "cam-1",
                "enabled": True,
                "recording_enabled": False,
            }
        )
        repo.create_camera(
            {
                "camera_id": "cam-disabled",
                "name": "비활성 카메라",
                "stream_path": "cam-disabled",
                "enabled": False,
                "recording_enabled": False,
            }
        )
        data_adapter = _DirectDataAdapter(repo, self.data_settings)

        # 1. Start recording on offline camera -> 'waiting' (녹화 대기)
        res_waiting = _run_async(
            _apply_camera_recording(
                self.request,
                self.external_settings,
                data_adapter,
                "cam-1",
                self.principal.user_id,
                True,
            )
        )
        self.assertEqual(res_waiting["recording_state"], "waiting")
        self.assertTrue(res_waiting["recording_enabled"])
        self.assertFalse(res_waiting["recording_file_active"])
        self.assertEqual(self.mediamtx.path_configs["cam-1"]["record"], True)

        # 2. Stream arrives for cam-1 -> actual file is created -> 'recording' (녹화 중)
        self.mediamtx.set_stream_active("cam-1", True)
        self.assertTrue(has_active_recording_file("cam-1", self.data_settings))
        listing = _run_async(
            list_cameras_recording(
                self.request, self.principal, self.external_settings, data_adapter
            )
        )
        by_id = {item["camera_id"]: item for item in listing["items"]}
        self.assertEqual(by_id["cam-1"]["recording_state"], "recording")
        self.assertTrue(by_id["cam-1"]["recording_file_active"])
        self.assertEqual(by_id["local-camera"]["recording_state"], "stopped")

        # 3. Connect local-camera stream and start all active cameras recording
        self.mediamtx.set_stream_active("local-camera", True)
        bulk_start = _run_async(
            _apply_bulk_camera_recording(
                self.request, self.external_settings, data_adapter, self.principal, True
            )
        )
        self.assertEqual(bulk_start["total"], 2)
        self.assertEqual(bulk_start["succeeded"], 2)
        self.assertEqual(bulk_start["failed"], 0)
        self.assertTrue(has_active_recording_file("local-camera", self.data_settings))
        self.assertTrue(has_active_recording_file("cam-1", self.data_settings))

        # 4. Stop recording on local-camera ONLY -> cam-1 is unaffected and continues recording
        stop_local = _run_async(
            _apply_camera_recording(
                self.request,
                self.external_settings,
                data_adapter,
                "local-camera",
                self.principal.user_id,
                False,
            )
        )
        self.assertEqual(stop_local["recording_state"], "stopped")
        self.assertFalse(has_active_recording_file("local-camera", self.data_settings))
        self.assertTrue(self.mediamtx.active_streams["local-camera"])
        self.assertTrue(has_active_recording_file("cam-1", self.data_settings))
        self.assertEqual(self.mediamtx.path_configs["cam-1"]["record"], True)

        # 5. Partial failure during bulk start when MediaMTX rejects cam-1
        self.mediamtx.fail_cameras.add("cam-1")
        bulk_partial = _run_async(
            _apply_bulk_camera_recording(
                self.request, self.external_settings, data_adapter, self.principal, True
            )
        )
        self.assertEqual(bulk_partial["total"], 2)
        self.assertEqual(bulk_partial["succeeded"], 1)
        self.assertEqual(bulk_partial["failed"], 1)
        partial_map = {item["camera_id"]: item for item in bulk_partial["items"]}
        self.assertTrue(partial_map["local-camera"]["ok"])
        self.assertEqual(partial_map["local-camera"]["recording_state"], "recording")
        self.assertFalse(partial_map["cam-1"]["ok"])
        self.assertEqual(partial_map["cam-1"]["recording_state"], "error")
        self.mediamtx.fail_cameras.clear()

        # 6. Server restart & MediaMTX reset -> sync_camera_recording_paths restores DB state
        _run_async(
            _apply_camera_recording(
                self.request,
                self.external_settings,
                data_adapter,
                "local-camera",
                self.principal.user_id,
                False,
            )
        )
        _run_async(
            _apply_camera_recording(
                self.request,
                self.external_settings,
                data_adapter,
                "cam-1",
                self.principal.user_id,
                True,
            )
        )
        self.mediamtx.path_configs.clear()

        restarted_repo = DataRepository(Database(self.db_path))
        restarted_adapter = _DirectDataAdapter(restarted_repo, self.data_settings)
        sync_stats = _run_async(
            sync_camera_recording_paths(
                self.external_settings,
                restarted_adapter,
                media_client_factory=self.request.app.state.media_client_factory,
            )
        )
        self.assertEqual(sync_stats["synced"], 3)
        self.assertEqual(self.mediamtx.path_configs["local-camera"]["record"], False)
        self.assertEqual(self.mediamtx.path_configs["cam-1"]["record"], True)
        self.assertEqual(self.mediamtx.path_configs["cam-disabled"]["record"], False)


class DesktopMonitoringRecordingGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.server_state = {
            "local-camera": {
                "camera_id": "local-camera",
                "name": "노트북 웹캠",
                "enabled": True,
                "recording_enabled": False,
                "recording_state": "stopped",
                "stream_ready": True,
                "recording_file_active": False,
                "last_error_code": None,
            },
            "cam-1": {
                "camera_id": "cam-1",
                "name": "정문 카메라",
                "enabled": True,
                "recording_enabled": True,
                "recording_state": "waiting",
                "stream_ready": False,
                "recording_file_active": False,
                "last_error_code": None,
            },
        }

    def _make_api_mock(self):
        state = self.server_state

        class _FakeApi:
            def cameras(self_api):
                return [dict(v) for v in state.values()]

            def cameras_recording(self_api):
                return {"items": [dict(v) for v in state.values()]}

            def start_camera_recording(self_api, camera_id):
                item = state[camera_id]
                item["recording_enabled"] = True
                item["recording_state"] = (
                    "recording" if item["stream_ready"] else "waiting"
                )
                return dict(item)

            def stop_camera_recording(self_api, camera_id):
                item = state[camera_id]
                item["recording_enabled"] = False
                item["recording_state"] = "stopped"
                return dict(item)

            def start_all_recording(self_api):
                items = []
                for _cid, item in state.items():
                    item["recording_enabled"] = True
                    item["recording_state"] = (
                        "recording" if item["stream_ready"] else "waiting"
                    )
                    items.append({**item, "ok": True})
                return {
                    "items": items,
                    "total": len(items),
                    "succeeded": len(items),
                    "failed": 0,
                }

            def stop_all_recording(self_api):
                items = []
                for _cid, item in state.items():
                    item["recording_enabled"] = False
                    item["recording_state"] = "stopped"
                    items.append({**item, "ok": True})
                return {
                    "items": items,
                    "total": len(items),
                    "succeeded": len(items),
                    "failed": 0,
                }

        return _FakeApi()

    def _wait_tasks(self, window: CCTVMainWindow):
        self.app.processEvents()

    def test_step3_and_step4_gui_buttons_status_badges_multi_gui_sync_and_live_video_persistence(
        self,
    ):
        class _SyncTask:
            def __init__(self, operation, _parent=None):
                self._operation = operation
                self._result_cbs = []
                self._failed_cbs = []
                self._finished_cbs = []
                self.result = SimpleNamespace(connect=self._result_cbs.append)
                self.failed = SimpleNamespace(connect=self._failed_cbs.append)
                self.finished = SimpleNamespace(connect=self._finished_cbs.append)

            def start(self):
                try:
                    value = self._operation(lambda _msg: None)
                    for cb in list(self._result_cbs):
                        cb(value)
                except Exception as exc:
                    for cb in list(self._failed_cbs):
                        cb(str(exc))
                finally:
                    for cb in list(self._finished_cbs):
                        cb()

            def deleteLater(self):
                pass

        class _DummyWorker:
            def __init__(self, source=None, **_kwargs):
                self.source = source
                self.running = True
                self.stopped = False
                self.frame_ready = SimpleNamespace(connect=lambda _cb: None)
                self.objects_ready = SimpleNamespace(connect=lambda _cb: None)
                self.metrics_ready = SimpleNamespace(connect=lambda _cb: None)
                self.event_ready = SimpleNamespace(connect=lambda _cb: None)
                self.loading_ready = SimpleNamespace(connect=lambda _cb: None)
                self.finished = SimpleNamespace(connect=lambda _cb: None)

            def start(self):
                self.running = True

            def stop(self):
                self.stopped = True
                self.running = False

            def acknowledge_frame(self):
                pass

            def deleteLater(self):
                pass

        with patch(
            "server.desktop.admin.application.VideoWorker", side_effect=_DummyWorker
        ), patch(
            "server.desktop.admin.application.BackgroundTask", side_effect=_SyncTask
        ), patch(
            "server.desktop.admin.application.WindowsSessionStore", return_value=None
        ):
            gui1 = CCTVMainWindow(allow_insecure_http=True)
            gui2 = CCTVMainWindow(allow_insecure_http=True)
            try:
                self.assertEqual(gui1.btn_start.text(), "전체 녹화 시작")
                self.assertEqual(gui1.btn_stop.text(), "전체 녹화 중지")

                gui1.api = self._make_api_mock()
                gui2.api = self._make_api_mock()
                with patch(
                    "server.desktop.admin.settings_window.list_camera_devices",
                    return_value=["Integrated Camera"],
                ):
                    settings = SettingsWindow(gui1)
                try:
                    self.assertTrue(hasattr(settings, "local_camera_enabled"))
                    self.assertTrue(hasattr(settings, "local_camera_device"))
                    self.assertFalse(hasattr(settings, "btn_stop_local_camera"))
                finally:
                    settings.close()
                gui1._view_mode = "grid"
                gui2._view_mode = "grid"
                gui1.cameras_updated(gui1.api.cameras())
                gui2.cameras_updated(gui2.api.cameras())
                self._wait_tasks(gui1)
                self._wait_tasks(gui2)

                self.assertIn("local-camera", gui1._workers)
                self.assertIn("cam-1", gui1._workers)
                local_worker = gui1._workers["local-camera"]
                cam1_worker = gui1._workers["cam-1"]

                frame = QImage(320, 180, QImage.Format_RGB888)
                frame.fill(0x22C55E)
                gui1._on_frame("local-camera", local_worker, frame)
                self.assertEqual(gui1._views["local-camera"].status.text(), "LIVE")

                self.assertEqual(
                    gui1._views["local-camera"].recording_status.text(), "녹화 중지"
                )
                self.assertEqual(
                    gui1._views["cam-1"].recording_status.text(), "녹화 대기"
                )
                self.assertEqual(
                    gui2._views["local-camera"].recording_status.text(), "녹화 중지"
                )
                self.assertEqual(
                    gui2._views["cam-1"].recording_status.text(), "녹화 대기"
                )

                gui1._views["local-camera"].btn_record_start.click()
                self._wait_tasks(gui1)
                self.assertEqual(
                    gui1._views["local-camera"].recording_status.text(), "녹화 중"
                )

                gui2.refresh_recording_states()
                self._wait_tasks(gui2)
                self.assertEqual(
                    gui2._views["local-camera"].recording_status.text(), "녹화 중"
                )

                self.server_state["cam-1"]["stream_ready"] = True
                self.server_state["cam-1"]["recording_state"] = "recording"
                gui1.refresh_recording_states()
                gui2.refresh_recording_states()
                self._wait_tasks(gui1)
                self._wait_tasks(gui2)
                self.assertEqual(
                    gui1._views["cam-1"].recording_status.text(), "녹화 중"
                )
                self.assertEqual(
                    gui2._views["cam-1"].recording_status.text(), "녹화 중"
                )

                gui1._views["local-camera"].btn_record_stop.click()
                self._wait_tasks(gui1)
                self.assertEqual(
                    gui1._views["local-camera"].recording_status.text(), "녹화 중지"
                )
                self.assertEqual(
                    gui1._views["cam-1"].recording_status.text(), "녹화 중"
                )
                self.assertFalse(local_worker.stopped)
                self.assertEqual(gui1._views["local-camera"].status.text(), "LIVE")

                with patch.object(gui1.local_publisher, "stop") as stop_local:
                    gui1.btn_stop.click()
                    self._wait_tasks(gui1)
                stop_local.assert_called_once_with()
                self.assertEqual(
                    gui1._views["local-camera"].recording_status.text(), "녹화 중지"
                )
                self.assertEqual(
                    gui1._views["cam-1"].recording_status.text(), "녹화 중지"
                )

                gui1._set_recording_notice("전체 녹화 제어 오류: Data 호출 실패")
                gui1._apply_recording_item(
                    {
                        "camera_id": "local-camera",
                        "enabled": True,
                        "recording_enabled": False,
                        "recording_state": "stopped",
                    }
                )
                self.assertEqual(
                    gui1.cam_status.text(), "전체 녹화 제어 오류: Data 호출 실패"
                )
                gui1.select_camera("cam-1")
                self.assertNotIn("전체 녹화 제어 오류", gui1.cam_status.text())

                gui1._apply_recording_item(
                    {
                        "camera_id": "cam-1",
                        "enabled": True,
                        "recording_enabled": True,
                        "recording_state": "error",
                        "error_code": "MEDIA_CONTROL_UNAVAILABLE",
                        "message": "MediaMTX 제어 API에 연결할 수 없습니다.",
                    }
                )
                self.assertEqual(
                    gui1._views["cam-1"].recording_status.toolTip(),
                    "MEDIA_CONTROL_UNAVAILABLE · MediaMTX 제어 API에 연결할 수 없습니다.",
                )
                self.assertFalse(local_worker.stopped)
                self.assertFalse(cam1_worker.stopped)
                self.assertEqual(gui1._views["local-camera"].status.text(), "LIVE")

                gui1._views["cam-1"].set_recording_state(
                    "error", error_code="MEDIA_CONTROL_UNAVAILABLE"
                )
                self.assertEqual(gui1._views["cam-1"].recording_status.text(), "오류")
                self.assertTrue(gui1._views["cam-1"].btn_record_start.isEnabled())
                self.assertTrue(gui1._views["cam-1"].btn_record_stop.isEnabled())
            finally:
                gui1._workers.clear()
                gui2._workers.clear()
                gui1.close()
                gui2.close()

    def test_desktop_api_recording_endpoints(self):
        calls = []
        api = DesktopApi("https://server.example", opener=lambda *_a, **_kw: None)
        api._request = lambda method, path, payload=None: (
            calls.append((method, path, payload)) or {}
        )

        api.cameras_recording()
        api.start_all_recording()
        api.stop_all_recording()
        api.camera_recording("cam-1")
        api.start_camera_recording("cam-1")
        api.stop_camera_recording("cam-1")

        self.assertEqual(
            calls,
            [
                ("GET", "/api/v1/cameras/recording", None),
                ("POST", "/api/v1/cameras/recording/start", None),
                ("POST", "/api/v1/cameras/recording/stop", None),
                ("GET", "/api/v1/cameras/cam-1/recording", None),
                ("POST", "/api/v1/cameras/cam-1/recording/start", None),
                ("POST", "/api/v1/cameras/cam-1/recording/stop", None),
            ],
        )


if __name__ == "__main__":
    unittest.main()
