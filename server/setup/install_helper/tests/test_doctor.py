"""호스트 진단의 공통 파일 검사·선택 모드·실행 상태를 검증한다."""

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from server.setup.install_helper import doctor
from server.setup.tests.test_validation import make_deployment, write_env
from server.setup.validation import read_deployment_env


# 별도 Python을 -S로 실행해 선택적 진단 모드가 외부 패키지와 Docker 없이 동작하는지 확인한다.
def test_source_cli_works_without_site_packages_or_docker(tmp_path):
    server, env, _, secrets = make_deployment(tmp_path)
    root = Path(__file__).resolve().parents[4]
    command = [
        sys.executable,
        "-S",
        "-m",
        "server.setup.install_helper.doctor",
        "--server-dir",
        str(server),
        "--env-file",
        str(env),
        "--skip-runtime",
        "--skip-compose",
    ]
    result = subprocess.run(
        command, cwd=root, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Configuration: schema" not in result.stdout
    assert "Compose configuration" not in result.stdout
    values = read_deployment_env(secrets["analysis"])
    values["DATA_ANALYSIS_TOKEN"] = "different-" + "x" * 40
    write_env(secrets["analysis"], values)
    result = subprocess.run(
        command, cwd=root, capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert "matches between data.env and analysis.env" in result.stdout
    assert values["DATA_ANALYSIS_TOKEN"] not in result.stdout + result.stderr


# Docker가 없어도 로컬 파일과 역할별 토큰의 불일치 진단은 계속 반환되어야 한다.
def test_missing_docker_does_not_hide_file_or_authentication_failures(
    tmp_path, monkeypatch
):
    server, env, config, secrets = make_deployment(tmp_path)
    values = read_deployment_env(secrets["analysis"])
    values["DATA_ANALYSIS_TOKEN"] = "m" * 40
    write_env(secrets["analysis"], values)
    monkeypatch.setattr(doctor.shutil, "which", lambda _: None)
    monkeypatch.setattr(doctor, "_configuration_checks", lambda *_: [])
    results = doctor.checks(server, config, env_file=env)
    assert any(
        result.name == "Docker Engine" and result.status == "ERROR"
        for result in results
    )
    assert any(
        result.status == "ERROR"
        and "matches between data.env and analysis.env" in result.message
        for result in results
    )


# 독립 doctor CLI의 소스 env 기본값과 설치 도우미가 호출하는 checks의 선택 경로를 구분한다.
def test_standalone_cli_defaults_to_source_env_but_checks_keep_installed_selection(
    tmp_path, monkeypatch, capsys
):
    server, env, config, _ = make_deployment(tmp_path)
    env.rename(server / ".env")
    installed_env = tmp_path / "installed" / "compose.env"
    monkeypatch.setenv("AI_CCTV_COMPOSE_ENV_FILE", str(installed_env))
    assert (
        doctor.main(["--server-dir", str(server), "--skip-runtime", "--skip-compose"])
        == 0
    )
    assert "[ERROR]" not in capsys.readouterr().out
    results = doctor.checks(server, config, skip_runtime=True, skip_compose=True)
    assert any(
        result.status == "ERROR" and str(installed_env) in result.message
        for result in results
    )


def test_skip_runtime_only_runs_compose_configuration(tmp_path, monkeypatch):
    server, env, config, _ = make_deployment(tmp_path)
    calls = []
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "docker")

    def run(adapter, *arguments, capture=False):
        calls.append(arguments)
        assert adapter.env_file == env.resolve()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(doctor.ComposeAdapter, "run", run)
    results = doctor.checks(server, config, env_file=env, skip_runtime=True)
    assert calls == [("config", "--quiet")]
    assert all(result.status == "OK" for result in results)


# 전체 진단에서 스키마·저장소·서비스·카메라 검사와 경고 수준이 함께 유지되어야 한다.
def test_full_diagnostics_retain_schema_storage_services_and_camera_checks(
    tmp_path, monkeypatch
):
    server, env, config, _ = make_deployment(tmp_path)
    root = Path(__file__).resolve().parents[4]
    config.write_bytes((root / "server/config/config.example.yaml").read_bytes())
    values = read_deployment_env(env)
    values.update(
        {
            "RECORDINGS_DIR": "./runtime/recordings",
            "MODEL_FILE": "test.pt",
            "AI_CCTV_UID": "0",
        }
    )
    write_env(env, values)
    (server / "runtime/models/test.pt").write_bytes(b"presence-only")
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "docker")
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="test compose", stderr=""
        ),
    )
    calls = []

    def run(_adapter, *arguments, capture=False):
        calls.append(arguments)
        if arguments[0] == "config":
            payload = ""
        elif arguments[0] == "ps":
            payload = "\n".join(
                json.dumps({"Service": name, "State": "running", "Health": "healthy"})
                for name in (
                    "data",
                    "external",
                    "preprocessing",
                    "analysis",
                    "mediamtx",
                    "nginx",
                )
            )
        else:
            payload = json.dumps(
                {
                    "workers": {
                        "cam-001": {"state": "online"},
                        "cam-002": {"state": "offline"},
                    }
                }
            )
        return SimpleNamespace(returncode=0, stdout=payload, stderr="")

    monkeypatch.setattr(doctor.ComposeAdapter, "run", run)
    results = doctor.checks(server, config, env_file=env)
    statuses = {result.name: result.status for result in results}
    assert statuses["Configuration"] == "OK"
    assert statuses["Recording storage"] == "OK"
    assert statuses["Inference model"] == "OK"
    assert statuses["Container runtime user"] == "WARN"
    assert statuses["Service analysis"] == "OK"
    assert statuses["Camera cam-001"] == "OK"
    assert statuses["Camera cam-002"] == "WARN"
    assert [call[0] for call in calls] == ["config", "ps", "exec"]


# Compose 구성이 유효하지 않으면 그 구성으로 ps나 내부 상태 조회를 실행하지 않아야 한다.
def test_compose_failure_prevents_runtime_queries(tmp_path, monkeypatch):
    server, env, config, _ = make_deployment(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "docker")
    monkeypatch.setattr(doctor, "_configuration_checks", lambda *_: [])
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
    )
    calls = []

    def run(_adapter, *arguments, capture=False):
        calls.append(arguments)
        return SimpleNamespace(returncode=1, stdout="", stderr="invalid compose")

    monkeypatch.setattr(doctor.ComposeAdapter, "run", run)
    results = doctor.checks(server, config, env_file=env)
    assert calls == [("config", "--quiet")]
    assert any(
        result.name == "Compose configuration" and result.status == "ERROR"
        for result in results
    )
