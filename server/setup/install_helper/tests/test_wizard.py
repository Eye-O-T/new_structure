"""설치 순서, 필수 조건 차단, 재실행과 실패 후 재시도를 실제 Qt 이벤트로 검증한다."""

import os
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QSettings
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from server.setup.install_helper import wizard
from server.setup.install_helper.compose_adapter import Prerequisite
from server.setup.install_helper.workflow import Installation


# 화면 없는 Qt 테스트 환경을 준비하고 Windows offscreen에서 필요한 글꼴·대화상자 표시만 보완한다.
@pytest.fixture(scope="module")
def app():
    from PyQt5.QtGui import QFont, QFontDatabase

    application = QApplication.instance() or QApplication([])
    # The Windows offscreen plugin exposes no system fonts in this environment.
    if not QFontDatabase().families():
        font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/malgun.ttf"
        if font_path.is_file():
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                application.setFont(QFont(families[0], 10))
    # QMessageBox.show/open crashes in the Windows offscreen plugin, even in an
    # isolated app without workers. Keep the real message object and assertions
    # for its text and blocked actions, but omit its native presentation here.
    with pytest.MonkeyPatch.context() as patcher:
        if application.platformName() == "offscreen":
            patcher.setattr(wizard.QMessageBox, "open", lambda _self: None)
        yield application


# Qt 이벤트를 계속 처리하면서 완료 조건을 기다려 신호 전달을 막지 않고 멈춘 작업을 탐지한다.
def wait_for(app, condition):
    deadline = time.monotonic() + 5
    while not condition():
        app.processEvents()
        QTest.qWait(10)
        assert time.monotonic() < deadline, "Qt 작업이 완료되지 않았습니다."
    app.processEvents()


def installed(root):
    return Installation(
        root,
        root / "config/compose.env",
        root / "config/config.yaml",
        "https://cctv.example.test:8443",
        "192.0.2.20",
        8555,
        "operator",
    )


# 네트워크 목록과 최근 설정을 격리하고 종료 전에 작업 완료를 기다려 QThread 수명을 보장한다.
@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setattr(
        wizard,
        "local_networks",
        lambda: [("LAN — 192.0.2.10", "192.0.2.10"), ("이 PC만", "127.0.0.1")],
    )
    preferences = QSettings(str(tmp_path / "ui.ini"), QSettings.IniFormat)
    value = wizard.InstallerWindow(
        data_root=tmp_path / "storage", preferences=preferences, auto_check=False
    )
    value.show()
    app.processEvents()
    yield value
    wait_for(app, lambda: not value._busy)
    if value._notice:
        value._notice.close()
    value.close()
    value.deleteLater()
    app.processEvents()


# 준비 검사 결과만 성공으로 대체하고 실제 비동기 완료 뒤 다음 버튼이 활성화되는지 확인한다.
def approve_preflight(window, app, monkeypatch):
    monkeypatch.setattr(
        wizard, "preflight", lambda *_: [Prerequisite(True, "준비", "확인")]
    )
    window.check_prerequisites()
    wait_for(app, lambda: not window._busy)
    assert window.next_button.isEnabled()


def set_password(window):
    window.password.setText("example-password-123")
    window.confirm_password.setText("example-password-123")


def test_missing_dependencies_block_next_and_file_change_requires_new_check(
    window, app, monkeypatch
):
    monkeypatch.setattr(
        wizard,
        "preflight",
        lambda *_: [
            Prerequisite(False, "Docker Desktop", "Docker Desktop을 설치해 주세요."),
            Prerequisite(False, "모델", "모델 파일을 선택하세요."),
        ],
    )
    window.check_prerequisites()
    wait_for(app, lambda: not window._busy)
    assert not window.next_button.isEnabled()
    assert "Docker Desktop" in window.check_results.toPlainText()
    assert "모델" in window._notice.text()
    window.go_next()
    assert window.pages.currentIndex() == 0
    window._notice.close()
    approve_preflight(window, app, monkeypatch)
    window.model.setText("changed.pt")
    assert not window.next_button.isEnabled()


# 기본값과 고급 설정이 요청까지 전달되며 검토 화면에는 관리자 암호가 나타나지 않아야 한다.
def test_default_next_flow_and_advanced_values_reach_install_request(
    window, app, monkeypatch
):
    monkeypatch.setattr(wizard, "_validate_request", lambda request: request.model_path)
    approve_preflight(window, app, monkeypatch)
    window.go_next()
    assert window.pages.currentIndex() == 1
    assert window.advanced.isHidden()
    set_password(window)
    window.go_next()
    request = window._request
    assert (request.public_http_port, request.public_https_port, request.rtsp_port) == (
        80,
        443,
        8554,
    )
    assert (
        request.retention_days,
        request.recording_segment_seconds,
        request.inference_device,
    ) == (7, 60, "auto")
    assert request.public_base_url == "https://192.0.2.10"
    assert request.public_bind_address == request.rtsp_bind_address == "192.0.2.10"
    assert request.admin_password not in window.review.toPlainText()
    assert window.next_button.text() == "설치 및 시작"
    window.go_back()
    window.advanced_toggle.setChecked(True)
    assert not window.advanced.isHidden()
    window.https.setValue(8443)
    window.rtsp_port.setValue(8555)
    window.retention_days.setValue(14)
    window.inference_device.setCurrentText("cuda:1")
    window.cameras.setText("cam-001:입구")
    window.go_next()
    request = window._request
    assert request.public_base_url == "https://192.0.2.10:8443"
    assert request.rtsp_port == 8555 and request.retention_days == 14
    assert request.inference_device == "cuda:1"
    assert request.cameras[0].name == "입구"


def test_password_confirmation_and_port_mismatch_stay_on_settings(
    window, app, monkeypatch
):
    monkeypatch.setattr(wizard, "_validate_request", lambda request: request.model_path)
    approve_preflight(window, app, monkeypatch)
    window.go_next()
    window.password.setText("example-password-123")
    window.go_next()
    assert window.pages.currentIndex() == 1
    assert "일치" in window._notice.text()
    set_password(window)
    window.public_base_url.setText("https://cctv.example.test:9443")
    window.go_next()
    assert window.pages.currentIndex() == 1
    assert "포트" in window._notice.text()


def test_network_selection_updates_defaults_but_preserves_custom_hostname(window):
    window.public_base_url.setText("https://cctv.example.test")
    window.network.setCurrentIndex(1)
    assert window.public_bind.text() == window.rtsp_bind.text() == "127.0.0.1"
    assert window.public_base_url.text() == "https://cctv.example.test"


# 기존 설치에서는 초기화를 호출하지 않고 기억한 저장 위치의 서비스 제어만 수행해야 한다.
def test_existing_installation_opens_management_and_remembers_location(
    window, app, monkeypatch
):
    root = Path(window.storage.text())
    deployment = installed(root)
    monkeypatch.setattr(wizard, "inspect_installation", lambda *_: deployment)
    monkeypatch.setattr(
        wizard, "install_new", lambda *_: pytest.fail("기존 설치가 초기화됨")
    )
    calls = []

    def operate(server_dir, selected, action, progress):
        assert selected.env_file == deployment.env_file
        calls.append(action)
        return "서버 상태를 확인했습니다."

    monkeypatch.setattr(wizard, "run_service_action", operate)
    window._load_location()
    assert window.pages.currentIndex() == 3
    assert window.next_button.isHidden()
    assert window.preferences.value("storage_root") == str(root)
    assert deployment.public_url in window.management_info.text()
    window.begin_installation()
    for action in ("status", "restart", "start", "stop"):
        window.service_action(action)
        wait_for(app, lambda: not window._busy)
    assert calls == ["status", "restart", "start", "stop"]
    second = wizard.InstallerWindow(preferences=window.preferences, auto_check=False)
    assert second.storage.text() == str(root)
    assert second.pages.currentIndex() == 3
    second.close()


def test_incomplete_installation_is_not_offered_new_setup(window, monkeypatch):
    def incomplete(*_):
        raise ValueError("기존 설정을 복구하세요.")

    monkeypatch.setattr(wizard, "inspect_installation", incomplete)
    window._load_location()
    assert window.pages.currentIndex() == 3
    assert window.installation is None
    assert window.next_button.isHidden()
    assert not any(button.isEnabled() for button in window.service_buttons)
    assert "복구" in window.management_output.toPlainText()


# 설정 저장은 성공하고 첫 기동만 실패하는 상황에서 재시도가 인증키를 다시 만들지 않는지 확인한다.
def test_failed_server_start_retries_without_regenerating_configuration(
    window, app, monkeypatch
):
    root = Path(window.storage.text())
    deployment = installed(root)
    creates = []
    starts = []

    def create(request):
        creates.append(request)
        root.mkdir()
        (root / "unchanged-token").write_text("original-token", encoding="utf-8")

    monkeypatch.setattr(wizard, "install_new", create)
    monkeypatch.setattr(
        wizard, "inspect_installation", lambda *_: deployment if creates else None
    )
    monkeypatch.setattr(wizard, "_validate_request", lambda request: request.model_path)

    def operate(server_dir, selected, action, progress):
        starts.append(action)
        if len(starts) == 1:
            raise RuntimeError("이미지를 준비하지 못했습니다. 다시 시도하세요.")
        return "서버 실행을 마쳤습니다."

    monkeypatch.setattr(wizard, "run_service_action", operate)
    approve_preflight(window, app, monkeypatch)
    window.go_next()
    set_password(window)
    window.go_next()
    window.go_next()
    wait_for(app, lambda: len(starts) == 1 and not window._busy)
    assert window.pages.currentIndex() == 3
    assert len(creates) == 1
    assert window.password.text() == ""
    window._notice.close()
    window.service_action("start")
    wait_for(app, lambda: not window._busy)
    assert len(starts) == 2 and len(creates) == 1
    assert (root / "unchanged-token").read_text() == "original-token"


# 작업을 대기 상태로 고정해 GUI 응답·중복 실행 차단·창 종료 보류를 검사한다.
def test_preflight_runs_in_background_and_disallows_duplicate_or_close(
    window, app, monkeypatch
):
    release = threading.Event()
    calls = []

    def slow(*_):
        calls.append(threading.current_thread())
        release.wait(3)
        return [Prerequisite(True, "준비", "확인")]

    monkeypatch.setattr(wizard, "preflight", slow)
    window.check_prerequisites()
    wait_for(app, lambda: len(calls) == 1)
    try:
        assert calls[0] is not threading.current_thread()
        window.check_prerequisites()
        assert len(calls) == 1
        assert not window.choose_storage_button.isEnabled()
        window.close()
        assert window.isVisible()
    finally:
        release.set()
    wait_for(app, lambda: not window._busy)
    assert window.next_button.isEnabled()
