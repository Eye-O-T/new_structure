# 실제 서버 없이 Qt 작업 경계와 일회성 게시 계정의 전달·복구 경로를 검증한다.
"""Exercise worker boundaries and real credential handoff without a live server."""

import json
import os
from pathlib import Path
import threading
import time

from PyQt5.QtCore import QTimer
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
import pytest

from server.setup.install_helper import edge_panel, server_api
from server.setup.install_helper.edge_discovery import DiscoveredEdge
from server.setup.install_helper.edge_pairing import EdgePairingError
from server.setup.install_helper.qt_tasks import BackgroundTask
from server.setup.install_helper.server_api import ServerApiError


PAIRING_KEY = "pairing-key-for-test-0123456789abcdef"
ADMIN_PASSWORD = "administrator-password-for-test"
PUBLISH_PASSWORD = "publish-password-for-test"


# 실제 Qt 신호와 이벤트 처리는 유지하면서 네이티브 화면이 없는 테스트 앱을 공유한다.
@pytest.fixture(scope="session")
def application():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


# Qt 이벤트를 처리하며 조건을 기다리고 제한 시간 안에 끝나지 않은 작업은 실패로 보고한다.
def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(10)
    assert predicate(), "Qt operation did not complete before the deadline"


# 테스트 서버·비밀 입력을 가진 패널을 준비하고 백그라운드 작업 종료 뒤 위젯을 정리한다.
@pytest.fixture
def panel(application, tmp_path, monkeypatch):
    monkeypatch.setattr(server_api, "restrict_private_file", lambda _path: None)
    widget = edge_panel.EdgePanel()
    widget.configure("https://cctv.example.com", "192.168.0.10", 8554, tmp_path)
    widget.username.setText("administrator")
    widget.password.setText(ADMIN_PASSWORD)
    widget.edge_auth_token.setText(PAIRING_KEY)
    widget.edge_name.setText("현관")
    yield widget
    wait_until(lambda: not widget.busy)
    widget.close()
    widget.deleteLater()
    application.processEvents()


def discovered() -> DiscoveredEdge:
    return DiscoveredEdge(
        device_id="edge-002",
        camera_id="cam-002",
        address="192.168.0.42",
        management_url="http://192.168.0.42:8003",
        recovery_url="http://192.168.0.42:8002",
        supported_profiles=("hd", "fhd"),
        message_id="test-message",
        sent_at=1,
    )


def select_edge(panel):
    edge = discovered()
    panel.discovered_edges = [edge]
    panel.discovered_edge_items.addItem("test Edge")
    panel.discovered_edge_items.setCurrentIndex(0)
    panel.select_discovered_edge(0)
    return edge


# 로그인 대기 지점과 스레드 ID를 기록하는 클라이언트로 UI 입력 스냅샷과 작업 스레드 경계를 검사한다.
def client_factory(calls, login_gate=None):
    class Client:
        def __init__(self, url):
            calls.append(("client", url, threading.get_ident()))

        def login(self, username, password):
            calls.append(("login", (username, password), threading.get_ident()))
            if login_gate is not None:
                assert login_gate.wait(2)

        def register_edge(self, **kwargs):
            calls.append(("register", kwargs, threading.get_ident()))
            return {
                "camera_id": kwargs["camera_id"],
                "publish_credentials": {
                    "username": kwargs["camera_id"],
                    "password": PUBLISH_PASSWORD,
                },
                "edge_auth_token": PAIRING_KEY,
            }

    return Client


# 작업이 GUI 밖에서 수행되고 완료 후 비밀값을 캡처할 수 있는 클로저 참조가 해제되는지 확인한다.
def test_background_task_reports_result_and_releases_operation(application):
    main_thread = threading.get_ident()
    outputs, progress = [], []

    def operation(report):
        report("작업 진행 중")
        return threading.get_ident()

    task = BackgroundTask(operation)
    task.result.connect(outputs.append)
    task.progress.connect(progress.append)
    task.start()
    wait_until(lambda: bool(outputs) and not task.isRunning())
    assert outputs[0] != main_thread
    assert progress == ["작업 진행 중"]
    assert task._operation is None
    task.wait()


# 검색을 잠시 멈춰도 Qt 타이머가 동작하고 중복 검색이 시작되지 않는지 확인한다.
def test_discovery_keeps_event_loop_responsive_and_blocks_duplicate_work(
    panel, monkeypatch
):
    calls, ticks, changes = [], [], []
    gate = threading.Event()

    def discover(key, timeout):
        calls.append((key, timeout, threading.get_ident()))
        assert gate.wait(2)
        return [discovered()]

    monkeypatch.setattr(edge_panel, "discover_edges", discover)
    panel.busy_changed.connect(changes.append)
    panel.discover_edge_devices()
    try:
        wait_until(lambda: bool(calls))
        assert panel.busy
        assert not panel.controls.isEnabled()
        QTimer.singleShot(0, lambda: ticks.append(True))
        wait_until(lambda: bool(ticks))
        panel.discover_edge_devices()
        assert len(calls) == 1
    finally:
        gate.set()
    wait_until(lambda: not panel.busy)
    assert calls == [(PAIRING_KEY, 3.0, calls[0][2])]
    assert calls[0][2] != threading.get_ident()
    assert changes == [True, False]
    assert panel.edge_camera_id.text() == "cam-002"
    assert panel.edge_management_url.text() == "http://192.168.0.42:8003"
    assert "cam-002-publish-credentials.json" in panel.publish_credentials_output.text()
    assert panel.advanced.isHidden()
    assert PAIRING_KEY not in panel.status.text()


# 작업 중 위젯을 프로그램으로 바꿔도 시작 당시 입력이 전송되며 성공 후 비밀 입력은 지워져야 한다.
def test_pairing_uses_gui_snapshot_and_never_displays_credentials(panel, monkeypatch):
    selected = select_edge(panel)
    calls, pairings = [], []
    gate = threading.Event()
    monkeypatch.setattr(edge_panel, "ServerApiClient", client_factory(calls, gate))
    monkeypatch.setattr(
        edge_panel,
        "complete_edge_pairing",
        lambda edge, **kwargs: pairings.append((edge, kwargs))
        or {"status": "configured"},
    )
    original_handoff = Path(panel.publish_credentials_output.text())
    panel.register_edge()
    try:
        wait_until(lambda: any(call[0] == "login" for call in calls))
        # Even programmatic edits while disabled must not change the running request.
        panel.edge_name.setText("변경된 이름")
        panel.edge_auth_token.setText("changed-key-0123456789abcdef0123456789")
        panel.register_edge()
    finally:
        gate.set()
    wait_until(lambda: not panel.busy)
    registration = next(call[1] for call in calls if call[0] == "register")
    assert registration["name"] == "현관"
    assert registration["edge_auth_token"] == PAIRING_KEY
    assert len([call for call in calls if call[0] == "register"]) == 1
    assert all(call[2] != threading.get_ident() for call in calls)
    assert pairings[0][0] == selected
    assert pairings[0][1]["pairing_key"] == PAIRING_KEY
    assert pairings[0][1]["central_host"] == "192.168.0.10"
    assert "연결이 완료" in panel.status.text()
    assert not original_handoff.exists()
    assert not panel.password.text()
    assert not panel.edge_auth_token.text()
    assert all(
        secret not in panel.status.text()
        for secret in (
            PAIRING_KEY,
            ADMIN_PASSWORD,
            PUBLISH_PASSWORD,
        )
    )


# 수동 등록과 자동 전달 실패 모두 중앙에서 발급한 같은 카메라의 계정을 파일로 남겨야 한다.
@pytest.mark.parametrize("automatic", [False, True])
def test_manual_and_failed_pairing_preserve_a_matching_handoff_file(
    panel, monkeypatch, automatic
):
    select_edge(panel)
    panel.manual_mode.setChecked(not automatic)
    calls = []
    monkeypatch.setattr(edge_panel, "ServerApiClient", client_factory(calls))

    def fail_pairing(*_args, **_kwargs):
        assert automatic, "manual registration must not provision an Edge"
        raise EdgePairingError(
            f"unexpected sensitive server message {PUBLISH_PASSWORD}"
        )

    monkeypatch.setattr(edge_panel, "complete_edge_pairing", fail_pairing)
    output = Path(panel.publish_credentials_output.text())
    panel.register_edge()
    wait_until(lambda: not panel.busy)
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "camera_id": "cam-002",
        "username": "cam-002",
        "password": PUBLISH_PASSWORD,
    }
    assert "중앙 등록이 완료" in panel.status.text()
    assert "다시 등록하지 마세요" in panel.status.text()
    assert PUBLISH_PASSWORD not in panel.status.text()


# 서버 오류 메시지에 암호가 섞여 있어도 화면은 고정된 권한 안내만 보여야 한다.
def test_server_failure_is_actionable_without_echoing_server_secrets(
    panel, monkeypatch
):
    select_edge(panel)

    class RejectingClient:
        def __init__(self, _url):
            pass

        def login(self, _username, _password):
            raise ServerApiError(403, "UNTRUSTED_ERROR", f"password={ADMIN_PASSWORD}")

    monkeypatch.setattr(edge_panel, "ServerApiClient", RejectingClient)
    panel.register_edge()
    wait_until(lambda: not panel.busy)
    assert "관리자 권한" in panel.status.text()
    assert ADMIN_PASSWORD not in panel.status.text()
    assert "UNTRUSTED_ERROR" not in panel.status.text()
    assert panel.controls.isEnabled()


def test_changed_discovered_identity_requires_explicit_manual_mode(panel, monkeypatch):
    select_edge(panel)
    panel.edge_device_id.setText("different-edge")
    calls = []
    monkeypatch.setattr(edge_panel, "ServerApiClient", client_factory(calls))
    panel.register_edge()
    assert not panel.busy
    assert calls == []
    assert "직접 등록" in panel.status.text()


# 잘못된 접속·백업·화질 입력은 서버 호출과 인계 파일 생성 전에 해당 입력에 초점을 돌려야 한다.
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("central_rtsp_host", "", "서버 IP 또는 호스트 이름"),
        ("central_rtsp_host", "0.0.0.0", "서버 IP 또는 호스트 이름"),
        ("central_rtsp_host", "::", "서버 IP 또는 호스트 이름"),
        ("central_rtsp_host", "https://192.168.0.10", "서버 IP 또는 호스트 이름"),
        ("edge_backup_root", "C:\\backup", "Pi의 절대 경로"),
        ("edge_backup_root", "recordings", "Pi의 절대 경로"),
        ("video_profile", "", "지원하는 영상 화질"),
    ],
)
def test_invalid_pairing_settings_focus_input_without_server_requests(
    panel, monkeypatch, application, field, value, message
):
    select_edge(panel)
    calls = []
    monkeypatch.setattr(edge_panel, "ServerApiClient", client_factory(calls))
    widget = getattr(panel, field)
    if field == "video_profile":
        widget.setCurrentIndex(-1)
    else:
        widget.setText(value)
    panel.show()
    application.processEvents()
    assert panel.advanced.isHidden()
    output = Path(panel.publish_credentials_output.text())
    panel.register_edge()
    assert not panel.busy
    assert calls == [], (
        "invalid input must not create a client or send login/register HTTP"
    )
    assert panel.advanced_toggle.isChecked()
    assert not panel.advanced.isHidden()
    assert panel.focusWidget() is widget
    assert message in panel.status.text()
    assert panel.password.text() == ADMIN_PASSWORD
    assert panel.edge_auth_token.text() == PAIRING_KEY
    assert not output.exists()


# 대상 서버가 바뀌면 검색 장치·연결 키·암호와 이전 서버 기준의 기본 인계 경로를 갱신해야 한다.
def test_switching_server_clears_previous_edge_and_credentials(panel, tmp_path):
    select_edge(panel)
    panel.manual_mode.setChecked(True)
    destination = tmp_path / "other-server"
    panel.configure("https://other.example.com", "192.168.0.20", 8555, destination)
    assert panel.discovered_edges == []
    assert panel.discovered_edge_items.currentIndex() == -1
    assert not panel.password.text()
    assert not panel.edge_auth_token.text()
    assert not panel.manual_mode.isChecked()
    assert not panel.edge_management_url.text()
    assert not panel.edge_recovery_url.text()
    assert panel.edge_camera_id.text() == "cam-001"
    assert Path(panel.publish_credentials_output.text()) == (
        destination / "secrets" / "cam-001-publish-credentials.json"
    )
    assert "다시 찾으세요" in panel.status.text()


def test_probe_runs_in_worker_and_only_reports_safe_status(panel, monkeypatch):
    selected = select_edge(panel)
    calls = []

    def probe(edge):
        calls.append((edge, threading.get_ident()))
        return {"status": "pairing", "unexpected_token": PAIRING_KEY}

    monkeypatch.setattr(edge_panel, "probe_edge_connection", probe)
    panel.test_selected_edge_connection()
    wait_until(lambda: not panel.busy)
    assert calls[0][0] == selected
    assert calls[0][1] != threading.get_ident()
    assert "정보가 일치" in panel.status.text()
    assert PAIRING_KEY not in panel.status.text()
