"""도구를 옮겨도 작업 폴더와 무관하게 원래 서버 배포를 선택하는지 확인한다."""

import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


# 임의 디렉터리에서 호출해도 기본 경로는 도구가 속한 배포 디렉터리를 기준으로 계산한다.
@pytest.mark.parametrize(
    ("module_name", "field", "relative_default"),
    [
        ("server.setup.tools.init_runtime", "runtime_root", "server/runtime"),
        ("server.setup.tools.generate_secrets", "output_dir", "server/secrets"),
        (
            "server.setup.tools.generate_dev_cert",
            "output_dir",
            "server/runtime/certificates",
        ),
        ("server.services.data.tools.backup_database", "server_dir", "server"),
        ("server.services.external.tools.bootstrap_admin", "server_dir", "server"),
    ],
)
def test_tool_defaults_do_not_follow_the_working_directory(
    module_name, field, relative_default, tmp_path, monkeypatch
):
    module = importlib.import_module(module_name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["tool"])
    assert getattr(module.parse_args(), field) == ROOT / relative_default


# PYTHONPATH 없이 문서의 파일 진입점으로 --help를 실행해 독립 사용과 무부작용을 확인한다.
@pytest.mark.parametrize(
    "relative_path",
    [
        "server/setup/tools/init_runtime.py",
        "server/setup/tools/generate_secrets.py",
        "server/setup/tools/generate_dev_cert.py",
        "server/setup/tools/enable_object_processing.py",
        "server/services/data/tools/backup_database.py",
        "server/services/external/tools/bootstrap_admin.py",
        "server/services/external/tools/export_openapi.py",
    ],
)
def test_documented_file_entry_points_work_outside_repository(relative_path, tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / relative_path), "--help"],
        cwd=tmp_path,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--help" in result.stdout
    assert list(tmp_path.iterdir()) == []


# Docker 호출만 가로채 선택한 서버의 compose 설정과 백업 본문이 전달되는지 확인한다.
def test_backup_remains_a_host_command_for_the_selected_server(tmp_path, monkeypatch):
    from server.services.data.tools import backup_database

    deployment = tmp_path / "selected server"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["backup", "--server-dir", str(deployment), "--filename", "before-change.db"],
    )
    monkeypatch.setattr(backup_database.shutil, "which", lambda _: "docker")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout='{"relative_path":"before-change.db"}')

    monkeypatch.setattr(backup_database.subprocess, "run", run)
    assert backup_database.main() == 0
    command, options = calls[0]
    assert command[:9] == [
        "docker", "compose", "--env-file", str(deployment / ".env"),
        "-f", str(deployment / "compose.yml"), "exec", "-T", "data",
    ]
    assert options["input"] == '{"filename": "before-change.db"}'
    assert not deployment.exists()
