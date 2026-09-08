"""개발 설정 변경이 운영 이미지·저장소와 섞이지 않는지 확인한다."""

from pathlib import Path
import tomllib

import yaml


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
        assert "pip install --no-cache-dir /opt/ai-cctv-core" in runtime
        assert "requirements-dev.txt" in stages
        assert stages.rstrip().endswith("FROM runtime AS production")
        service = development["services"][name]
        assert service["build"]["target"] == "development"
        assert service["image"].endswith("-dev")
        assert service["volumes"][0]["read_only"] is True
        assert "--reload" in service["command"]


def test_python_package_ownership_is_explicit():
    for name in (
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        "requirements-dev.txt",
    ):
        assert not Path(name).exists()
    core = tomllib.loads(Path("lib/pyproject.toml").read_text(encoding="utf-8"))
    gui = tomllib.loads(Path("configurator/pyproject.toml").read_text(encoding="utf-8"))
    assert core["project"]["name"] == "ai-cctv-core"
    assert "test" not in core["project"].get("optional-dependencies", {})
    assert gui["tool"]["uv"]["sources"]["ai-cctv-core"]["path"] == "../lib"
    assert gui["tool"]["setuptools"]["packages"] == ["configurator"]
    assert Path("configurator/uv.lock").is_file()
