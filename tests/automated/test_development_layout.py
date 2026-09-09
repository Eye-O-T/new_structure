"""개발 설정 변경이 운영 이미지·저장소와 섞이지 않는지 확인한다."""

from pathlib import Path
import tomllib

import yaml


# 테스트 컨테이너의 네트워크·쓰기 영역을 제한하고 운영 비밀값·볼륨 상속을 막는다.
def test_test_stack_does_not_inherit_deployment_secrets_or_storage():
    stack = yaml.safe_load(Path("server/compose.test.yml").read_text(encoding="utf-8"))
    assert stack["networks"]["test"]["internal"] is True
    for service in stack["services"].values():
        assert not service.get("env_file")
        assert not service.get("volumes")
        assert not service.get("ports")
        assert service["read_only"] is True
        assert "/tmp" in service["tmpfs"]
    assert stack["services"]["external-test"]["environment"]["PUSH_ENABLED"] == "false"
    assert (
        stack["services"]["integration"]["depends_on"]["external-test"]["condition"]
        == "service_healthy"
    )


# 개발 도구와 코드 재로딩은 development 단계에만 있고 production은 runtime에서 끝나야 한다.
def test_runtime_and_development_images_keep_separate_dependencies():
    development = yaml.safe_load(
        Path("server/compose.dev.yml").read_text(encoding="utf-8")
    )
    for name in ("data", "external", "preprocessing", "analysis"):
        dockerfile = Path(f"server/services/{name}/Dockerfile").read_text(
            encoding="utf-8"
        )
        runtime, stages = dockerfile.split("FROM runtime AS development", 1)
        assert "requirements-dev.txt" not in runtime
        assert "/opt/ai-cctv-core" in runtime
        assert "requirements-dev.txt" in stages
        assert stages.rstrip().endswith("FROM runtime AS production")
        service = development["services"][name]
        assert service["build"]["target"] == "development"
        assert service["image"].endswith("-dev")
        assert service["volumes"][0]["read_only"] is True
        assert "--reload" in service["command"]


# 루트에 통합 의존성을 만들지 않고 공용 라이브러리·설치 도구·GUI의 소유 경계를 유지한다.
def test_python_package_ownership_is_explicit():
    for name in (
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        "requirements-dev.txt",
    ):
        assert not Path(name).exists()
    core = tomllib.loads(Path("lib/pyproject.toml").read_text(encoding="utf-8"))
    gui = tomllib.loads(Path("server/setup/install_helper/pyproject.toml").read_text(encoding="utf-8"))
    assert core["project"]["name"] == "ai-cctv-core"
    assert "test" not in core["project"].get("optional-dependencies", {})
    assert gui["tool"]["uv"]["sources"]["ai-cctv-core"]["path"] == "../../../lib"
    assert gui["tool"]["setuptools"]["packages"] == ["server.setup.install_helper"]
    assert Path("server/setup/install_helper/uv.lock").is_file()

    setup = tomllib.loads(Path("server/setup/pyproject.toml").read_text(encoding="utf-8"))
    assert setup["tool"]["setuptools"]["packages"] == [
        "server.setup", "server.setup.tools"
    ]
    assert gui["tool"]["uv"]["sources"]["ai-cctv-server-setup"]["path"] == ".."
    assert "PyQt5" not in " ".join(setup["project"]["dependencies"])
