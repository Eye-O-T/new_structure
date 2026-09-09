"""Docker를 실행하지 않고 대상 배포 보호·출력 제한·시간 초과 정리를 검증한다."""

import json
import subprocess

import pytest

from server.setup.install_helper import host_actions
from server.setup.install_helper.compose_adapter import START_ARGUMENTS, Prerequisite
from server.setup.install_helper.workflow import Installation


# 선택한 설정·DB 경로가 담긴 최소 배포를 만들어 컨테이너 식별 대조의 기준으로 사용한다.
@pytest.fixture
def installed(tmp_path):
    root = tmp_path / "selected data"
    (root / "config").mkdir(parents=True)
    (root / "database").mkdir()
    config = root / "config" / "config.yaml"
    config.write_text("schema_version: 1", encoding="utf-8")
    env = root / "config" / "compose.env"
    env.write_text(
        f"CONFIG_FILE='{config}'\nDATABASE_DIR='{root / 'database'}'\n",
        encoding="utf-8",
    )
    server = tmp_path / "server"
    server.mkdir()
    return server, Installation(
        root, env, config, "https://example.com", "127.0.0.1", 8554, "admin"
    )


# Compose 설정과 inspect 응답을 순서대로 만들며 다른 저장소·빈 배포·Data 누락 상황을 재현한다.
def identity_output(installation, *, source_root=None, empty=False, missing_data=False):
    expected_config = str(installation.config_file)
    expected_database = str(installation.data_root / "database")
    configuration = {
        "name": "ai-cctv",
        "services": {
            "data": {
                "environment": {"PRIVATE_TOKEN": "do-not-display-this-token"},
                "volumes": [
                    {
                        "type": "bind",
                        "source": expected_config,
                        "target": "/app/config/config.yaml",
                    },
                    {
                        "type": "bind",
                        "source": expected_database,
                        "target": "/data/database",
                    },
                ],
            }
        },
    }
    if empty:
        return [json.dumps(configuration), ""]
    if missing_data:
        return [json.dumps(configuration), "bbbbbbbbbbbb\n", ""]
    source_config = (
        source_root / "config" / "config.yaml"
        if source_root
        else installation.config_file
    )
    source_database = (
        source_root / "database" if source_root else installation.data_root / "database"
    )
    mounts = [
        {
            "Type": "bind",
            "Source": str(source_config),
            "Destination": "/app/config/config.yaml",
        },
        {
            "Type": "bind",
            "Source": str(source_database),
            "Destination": "/data/database",
        },
    ]
    return [
        json.dumps(configuration),
        "aaaaaaaaaaaa\nbbbbbbbbbbbb\n",
        "aaaaaaaaaaaa\n",
        json.dumps(mounts),
    ]


# 실제 프로세스를 시작하지 않고 정상 종료·시간 초과·강제 종료 상태와 전달된 제한 시간을 기록한다.
class FakeProcess:
    def __init__(self, output, *, returncode=0, timeout=False):
        self.output = output
        self.returncode = returncode
        self.pid = 43210
        self.timeout = timeout
        self.killed = False
        self.timeouts = []

    def communicate(self, timeout):
        self.timeouts.append(timeout)
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired(
                "private-command-token", timeout, output="private-output"
            )
        return self.output, None

    def poll(self):
        return None if self.timeout and not self.killed else self.returncode

    def kill(self):
        self.killed = True


# 조회 응답을 큐로 공급하고 실행 인수를 기록해 실제 Docker 없이 조회·변경 순서를 검증한다.
@pytest.fixture
def processes(monkeypatch):
    queue = []
    calls = []

    def popen(command, **options):
        assert queue, f"unexpected process: {command}"
        result = queue.pop(0)
        if isinstance(result, str):
            result = FakeProcess(result)
        calls.append((command, options, result))
        return result

    monkeypatch.setattr(host_actions.subprocess, "Popen", popen)
    monkeypatch.setattr(
        host_actions.ComposeAdapter,
        "deployment_prerequisites",
        lambda _: [Prerequisite(True, "ready", "ready")],
    )
    return queue, calls


@pytest.mark.parametrize(
    "actual,expected",
    [
        (r"C:\ProgramData\AI_CCTV\database", "c:/programdata/ai_cctv/database"),
        ("/host_mnt/c/ProgramData/AI_CCTV/database", "c:/programdata/ai_cctv/database"),
        (
            "/run/desktop/mnt/host/C/ProgramData/AI_CCTV/database",
            "c:/programdata/ai_cctv/database",
        ),
        (r"\\?\C:\ProgramData\AI_CCTV\database", "c:/programdata/ai_cctv/database"),
        ("/var/lib/AI_CCTV/database", "/var/lib/AI_CCTV/database"),
    ],
)
def test_normalize_host_paths_without_losing_linux_case(actual, expected):
    assert host_actions._host_path(actual) == expected


# 프로젝트명이 같아도 Data 마운트가 다른 배포에는 기동·재시작·중지 명령을 보내면 안 된다.
@pytest.mark.parametrize("action", ["start", "restart", "stop"])
def test_another_deployment_is_never_changed(installed, processes, action, tmp_path):
    server, deployment = installed
    queue, calls = processes
    queue.extend(
        identity_output(deployment, source_root=tmp_path / "other installation")
    )
    with pytest.raises(RuntimeError, match="다른 배포") as error:
        host_actions.run_service_action(server, deployment, action, lambda _: None)
    assert "do-not-display" not in str(error.value)
    assert len(calls) == 4
    assert not queue


def test_status_explains_different_target_without_showing_its_service_state(
    installed, processes, tmp_path
):
    server, deployment = installed
    queue, calls = processes
    queue.extend(identity_output(deployment, source_root=tmp_path / "other"))
    result = host_actions.run_service_action(
        server, deployment, "status", lambda _: None
    )
    assert "다른 배포" in result
    assert len(calls) == 4


@pytest.mark.parametrize("action", ["start", "restart", "stop"])
def test_orphaned_containers_without_data_cannot_be_modified(
    installed, processes, action
):
    server, deployment = installed
    queue, calls = processes
    queue.extend(identity_output(deployment, missing_data=True))
    with pytest.raises(RuntimeError, match="저장 위치를 확인할 수 없어"):
        host_actions.run_service_action(server, deployment, action, lambda _: None)
    assert len(calls) == 3


# 빈 배포는 기동할 수 있지만 빌드 출력과 임의 서비스·상태 문자열은 사용자에게 노출하지 않는다.
def test_no_existing_containers_can_start_and_only_allowlisted_status_is_displayed(
    installed, processes
):
    server, deployment = installed
    queue, calls = processes
    queue.extend(identity_output(deployment, empty=True))
    queue.append("private-build-output-password")
    queue.append(
        json.dumps(
            [
                {
                    "Service": "data",
                    "State": "running",
                    "Health": "healthy",
                    "Name": "do-not-display-token",
                },
                {
                    "Service": "injected-secret",
                    "State": "other-secret",
                    "Health": "health-secret",
                },
            ]
        )
    )
    progress = []
    result = host_actions.run_service_action(
        server, deployment, "start", progress.append
    )
    assert "데이터 저장: 실행 중 / 정상" in result
    assert "알 수 없는 서비스: 상태 확인 필요" in result
    assert "secret" not in result and "password" not in result and "token" not in result
    assert calls[2][0][-len(START_ARGUMENTS) :] == list(START_ARGUMENTS)
    assert calls[2][1]["stdout"] == subprocess.DEVNULL
    assert all(options["stderr"] == subprocess.DEVNULL for _, options, _ in calls)
    assert all(0 < call[2].timeouts[0] <= 1800 for call in calls)
    assert len(progress) == 4


@pytest.mark.parametrize("action", ["restart", "stop", "status"])
def test_no_containers_need_no_restart_stop_or_status_process(
    installed, processes, action
):
    server, deployment = installed
    queue, calls = processes
    queue.extend(identity_output(deployment, empty=True))
    result = host_actions.run_service_action(server, deployment, action, lambda _: None)
    assert "컨테이너가 없습니다" in result
    assert len(calls) == 2
    assert all(0 < call[2].timeouts[0] <= 120 for call in calls)


@pytest.mark.parametrize("action", ["restart", "stop"])
def test_matching_deployment_can_be_changed_without_reinitializing(
    installed, processes, action
):
    server, deployment = installed
    queue, calls = processes
    queue.extend(identity_output(deployment))
    queue.append("do-not-display-operation-secret")
    if action == "restart":
        queue.append('{"Service":"nginx","State":"running","Health":""}\n')
    before = deployment.env_file.read_bytes()
    result = host_actions.run_service_action(server, deployment, action, lambda _: None)
    assert calls[4][0][-1] == ("restart" if action == "restart" else "down")
    assert "do-not-display" not in result
    assert deployment.env_file.read_bytes() == before


# 컨테이너가 아직 없어도 Compose의 실제 마운트가 선택한 파일과 다르면 기동을 막아야 한다.
def test_effective_compose_mounts_must_match_selected_files_even_without_containers(
    installed, processes
):
    server, deployment = installed
    queue, calls = processes
    configuration = json.loads(identity_output(deployment, empty=True)[0])
    configuration["services"]["data"]["volumes"][0]["source"] = "/other/config.yaml"
    queue.append(json.dumps(configuration))
    with pytest.raises(RuntimeError, match="실행 구성이 선택한"):
        host_actions.run_service_action(server, deployment, "start", lambda _: None)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "bad_result", ["malformed-private-token", '{"private":"token"}']
)
def test_docker_query_errors_never_allow_changes_or_expose_raw_values(
    installed, processes, bad_result
):
    server, deployment = installed
    queue, calls = processes
    queue.append(bad_result)
    with pytest.raises(RuntimeError) as error:
        host_actions.run_service_action(server, deployment, "start", lambda _: None)
    assert "private" not in str(error.value)
    assert "token" not in str(error.value)
    assert len(calls) == 1


def test_failed_query_and_failed_mutation_do_not_expose_process_output(
    installed, processes
):
    server, deployment = installed
    queue, calls = processes
    queue.append(FakeProcess("do-not-display-token", returncode=1))
    with pytest.raises(RuntimeError) as error:
        host_actions.run_service_action(server, deployment, "stop", lambda _: None)
    assert "do-not-display" not in str(error.value)
    assert len(calls) == 1
    queue.extend(identity_output(deployment))
    queue.append(FakeProcess("do-not-display-token", returncode=1))
    with pytest.raises(RuntimeError) as error:
        host_actions.run_service_action(server, deployment, "stop", lambda _: None)
    assert "do-not-display" not in str(error.value)


# 시간 초과 시 이번 명령의 Windows 프로세스 트리를 정리하고 Docker 반영 가능성은 안내한다.
def test_timeout_terminates_windows_process_tree_and_reports_possible_partial_work(
    installed, processes, monkeypatch
):
    server, deployment = installed
    queue, _ = processes
    queue.extend(identity_output(deployment))
    timed_out = FakeProcess("private-output", timeout=True)
    queue.append(timed_out)
    monkeypatch.setattr(host_actions, "_WINDOWS", True)
    terminations = []

    def terminate(command, **options):
        terminations.append((command, options))
        timed_out.killed = True
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(host_actions.subprocess, "run", terminate)
    with pytest.raises(RuntimeError, match="제한 시간") as error:
        host_actions.run_service_action(server, deployment, "stop", lambda _: None)
    assert "일부 작업이 반영" in str(error.value)
    assert "private" not in str(error.value)
    assert terminations[0][0] == ["taskkill", "/PID", "43210", "/T", "/F"]
    assert terminations[0][1]["timeout"] == 10
    assert timed_out.killed


# POSIX에서는 새 세션으로 시작한 해당 프로세스 그룹만 종료하는지 확인한다.
def test_timeout_terminates_only_its_posix_process_group(
    installed, processes, monkeypatch
):
    server, deployment = installed
    queue, calls = processes
    queue.extend(identity_output(deployment))
    timed_out = FakeProcess("private-output", timeout=True)
    queue.append(timed_out)
    monkeypatch.setattr(host_actions, "_WINDOWS", False)
    monkeypatch.setattr(host_actions.signal, "SIGKILL", 9, raising=False)
    groups = []

    def killpg(pid, selected_signal):
        groups.append((pid, selected_signal))
        timed_out.killed = True

    monkeypatch.setattr(host_actions.os, "killpg", killpg, raising=False)
    with pytest.raises(RuntimeError, match="제한 시간"):
        host_actions.run_service_action(server, deployment, "restart", lambda _: None)
    assert groups == [(43210, host_actions.signal.SIGKILL)]
    assert calls[-1][1]["start_new_session"] is True


# 후속 조회마다 시간을 새로 주지 않고 같은 작업 마감 시각에서 남은 시간을 계산하는지 확인한다.
def test_total_deadline_is_shared_between_queries(installed, processes, monkeypatch):
    server, deployment = installed
    queue, calls = processes
    queue.extend(identity_output(deployment, empty=True))
    moments = iter([100.0, 200.0, 211.0])
    monkeypatch.setattr(host_actions.time, "monotonic", lambda: next(moments))
    host_actions.run_service_action(server, deployment, "status", lambda _: None)
    assert [process.timeouts for _, _, process in calls] == [[20.0], [9.0]]


def test_start_prerequisite_messages_are_not_exposed(installed, processes, monkeypatch):
    server, deployment = installed
    queue, calls = processes
    queue.extend(identity_output(deployment, empty=True))
    monkeypatch.setattr(
        host_actions.ComposeAdapter,
        "deployment_prerequisites",
        lambda _: [Prerequisite(False, "private-token", "private-token")],
    )
    with pytest.raises(RuntimeError) as error:
        host_actions.run_service_action(server, deployment, "start", lambda _: None)
    assert "private" not in str(error.value)
    assert len(calls) == 2
