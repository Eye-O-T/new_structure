# Windows 설치 정의를 읽어 배포 입력·경로·업그레이드 정리·운영 데이터 보존을 검사한다.
from fnmatch import fnmatchcase
from pathlib import Path, PureWindowsPath
import re
import runpy
import sys
from types import ModuleType, SimpleNamespace

import pytest


PACKAGING = Path("server/setup/install_helper/packaging")


# Inno Setup 섹션에서 인용된 필드만 추출해 배포 입력과 정리 범위를 검사한다.
def _installer_entries(section: str) -> list[dict[str, str]]:
    installer = (PACKAGING / "AI_CCTV_Server.iss").read_text(encoding="utf-8")
    content = installer.split(f"[{section}]", 1)[1].split("\n[", 1)[0]
    return [
        dict(re.findall(r'(\w+):\s*(?:"([^"]*)")', line))
        for line in content.splitlines()
        if line and not line.startswith(";")
    ]


def test_windows_packaging_sources_are_valid_utf8_without_replacement_text():
    for path in (
        PACKAGING / "AI_CCTV_Server.iss",
        Path("README.md"),
    ):
        text = path.read_text(encoding="utf-8")
        assert "\ufffd" not in text, path


# 실행 파일·Compose 빌드 입력·문서는 포함하고 개발 자료와 실제 비밀 파일은 제외하는지 확인한다.
def test_inno_installer_packages_gui_cli_and_required_compose_context():
    installer = (PACKAGING / "AI_CCTV_Server.iss").read_text(encoding="utf-8")

    assert (
        'Source: "..\\..\\..\\..\\dist\\AI_CCTV_Server_Install_Helper.exe"' in installer
    )
    assert 'Source: "..\\..\\..\\..\\dist\\AI_CCTV_CLI.exe"' in installer
    assert 'Source: "..\\..\\..\\..\\.dockerignore"' in installer
    assert 'Source: "..\\..\\..\\..\\lib\\*"' in installer
    assert 'Source: "..\\..\\..\\..\\server\\*"' in installer
    # 설치 안내의 위치가 바뀌어도 패키징 입력과 바로가기가 실제 문서를 가리켜야 한다.
    assert 'Source: "..\\..\\..\\..\\README.md"; DestDir: "{app}"' in installer
    assert (
        'Source: "..\\..\\..\\..\\mobile\\README.md"; DestDir: "{app}\\mobile"'
        in installer
    )
    assert 'Parameters: """{app}\\README.md"""' in installer
    for document in (
        "README.md",
        "guide.md",
        "operations.md",
        "architecture.md",
        "openapi.yaml",
    ):
        assert f'Source: "..\\..\\..\\..\\docs\\{document}"' in installer
        assert Path("docs", document).is_file()
    assert "docs\\SRS_interface_" not in installer
    asset_rule = next(
        line
        for line in installer.splitlines()
        if line.startswith(
            'Source: "..\\..\\..\\..\\docs\\assets\\architecture\\*.svg"'
        )
    )
    assert 'DestDir: "{app}\\docs\\assets\\architecture"' in asset_rule
    assert "recursesubdirs" in asset_rule
    assert "createallsubdirs" in asset_rule
    assert "secrets\\*" in installer
    assert "runtime\\*" in installer
    assert "config\\config.yaml" in installer
    assert "__pycache__\\*" in installer
    assert "*.key" in installer
    assert "*.pem" in installer
    assert "*.egg-info\\*" in installer
    for development_input in (
        "tests\\*",
        "setup\\tests\\*",
        "setup\\*.egg-info\\*",
        "setup\\install_helper\\*",
        ".venv\\*",
        "compose.dev.yml",
        "compose.test.yml",
        "services\\external\\tools\\export_openapi.py",
    ):
        assert development_input in installer

    server_source = next(
        line
        for line in installer.splitlines()
        if line.startswith('Source: "..\\..\\..\\..\\server\\*"')
    )
    for private_input in ("secrets\\*", "*.env", "*.bak", "runtime\\*"):
        assert private_input in server_source
    assert (
        "server/secrets/*.json"
        in Path(".gitignore").read_text(encoding="utf-8").splitlines()
    )


# 제거 시 영구 데이터가 남고 프로그램 폴더의 쓰기 권한이 일반 사용자에게 풀리지 않아야 한다.
def test_inno_installer_preserves_runtime_data_without_opening_program_files():
    installer = (PACKAGING / "AI_CCTV_Server.iss").read_text(encoding="utf-8")

    data_dir = next(
        line
        for line in installer.splitlines()
        if line.startswith('Name: "{commonappdata}\\AI_CCTV"')
    )
    assert "uninsneveruninstall" in data_dir
    assert "users-modify" not in data_dir
    assert 'Name: "{app}\\server"; Permissions:' not in installer
    assert "runasoriginaluser" not in installer
    assert 'Type: files; Name: "{app}\\server\\.env"' in installer
    assert 'Type: filesandordirs; Name: "{commonappdata}' not in installer
    assert 'Type: files; Name: "{commonappdata}' not in installer
    # 같은 앱으로 업그레이드하되 이전 이름의 실행 파일과 바로가기만 정리한다.
    assert "AppId={{9C5AB807-BB88-4929-982C-D5C7B92D62EA}" in installer
    assert "DefaultDirName={autopf}\\AI_CCTV" in installer
    assert "[InstallDelete]" in installer
    assert 'Type: files; Name: "{app}\\AI_CCTV_Configurator.exe"' in installer
    assert 'Type: files; Name: "{group}\\AI CCTV Configurator.lnk"' in installer


def test_inno_installer_has_consumer_entrypoints_and_safe_uninstall():
    installer = (PACKAGING / "AI_CCTV_Server.iss").read_text(encoding="utf-8")

    assert "AI CCTV Server Install Helper" in installer
    assert "AI CCTV CLI Console" in installer
    assert "addtopath" in installer
    assert "AddInstallDirToPath" in installer
    assert "RemoveInstallDirFromPath" in installer
    assert "[UninstallRun]" in installer
    assert "AI_CCTV_CLI.exe" in installer
    assert "--env-file" in installer
    assert " down -v" not in installer


def test_windows_build_script_builds_both_entrypoints_and_checksum():
    script = (PACKAGING / "build_windows_installer.ps1").read_text(encoding="utf-8")

    assert "ai_cctv_install_helper.spec" in script
    assert "ai_cctv_cli.spec" in script
    assert "AI_CCTV_Server_Install_Helper.exe" in script
    assert "AI_CCTV_CLI.exe" in script
    assert "ISCC.exe" in script
    assert "Get-FileHash" in script
    assert "SHA256" in script
    assert "$env:OS -ne 'Windows_NT'" in script
    assert "$IsWindows" not in script
    assert "'sync', '--locked'" in script
    assert "'--extra', 'test', '--extra', 'build'" in script
    assert "$env:UV_PROJECT_ENVIRONMENT = $buildVenv" in script
    assert "@('--check', '--offline')" in script
    assert "requirements-windows-build.txt" not in script
    assert "'pip', 'install'" not in script
    for document in (
        "README.md",
        "mobile\\README.md",
        "server\\setup\\install_helper\\uv.lock",
        "docs\\architecture.md",
        "docs\\operations.md",
        "server\\tools\\export_release_manifest.py",
        "docs\\openapi.yaml",
    ):
        assert f"(Join-Path $repositoryRoot '{document}')" in script
        assert Path(document.replace("\\", "/")).is_file()
    assert "SRS_interface_" not in script


# PyInstaller 객체만 대체해 spec을 실행하고 현재 디렉터리와 무관한 입력 경로·콘솔·UAC 설정을 검사한다.
@pytest.mark.parametrize(
    ("name", "entrypoint", "console"),
    [
        ("ai_cctv_install_helper.spec", "gui_entry.py", False),
        ("ai_cctv_cli.spec", "cli_entry.py", True),
    ],
)
def test_pyinstaller_specs_resolve_sources_from_any_working_directory(
    name, entrypoint, console, tmp_path, monkeypatch
):
    repository_root = Path.cwd()
    packaging = PACKAGING.resolve()
    calls = {}
    hooks = ModuleType("PyInstaller.utils.hooks")
    hooks.collect_submodules = lambda _name: []
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)

    def analysis(scripts, **kwargs):
        calls["scripts"] = scripts
        calls["analysis"] = kwargs
        return SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])

    def executable(*_args, **kwargs):
        calls["executable"] = kwargs

    monkeypatch.chdir(tmp_path)
    runpy.run_path(
        str(packaging / name),
        init_globals={
            "SPECPATH": str(packaging),
            "Analysis": analysis,
            "PYZ": lambda _pure: None,
            "EXE": executable,
        },
    )

    assert calls["scripts"] == [
        str(repository_root / "server" / "setup" / "install_helper" / entrypoint)
    ]
    assert Path(calls["scripts"][0]).is_file()
    assert calls["analysis"]["pathex"] == [
        str(repository_root),
        str(repository_root / "lib"),
    ]
    assert calls["executable"]["console"] is console
    if not console:
        assert calls["executable"]["uac_admin"] is True


def test_inno_sources_resolve_to_existing_repository_inputs():
    repository_root = Path.cwd()
    for entry in _installer_entries("Files"):
        source = PureWindowsPath(entry["Source"])
        resolved = (PACKAGING / Path(*source.parts)).resolve()
        # 실행 파일은 PyInstaller가 빌드하며 나머지는 저장소에 있는 실제 입력이다.
        if resolved.suffix == ".exe":
            assert resolved.parent == repository_root / "dist"
        elif resolved.name == "*":
            assert resolved.parent.is_dir()
        elif resolved.name == "*.svg":
            assets = list(resolved.parent.rglob(resolved.name))
            assert assets, "architecture diagrams must be included in the installer"
            installed_parent = PureWindowsPath(entry["DestDir"])
            for asset in assets:
                installed_relative = Path(
                    *installed_parent.parts[1:]
                ) / asset.relative_to(resolved.parent)
                assert installed_relative == asset.relative_to(repository_root)
            architecture = Path("docs/architecture.md").read_text(encoding="utf-8")
            referenced_images = re.findall(
                r"!\[[^\]]*\]\((assets/architecture/[^)]+\.svg)\)", architecture
            )
            assert referenced_images, (
                "architecture document must reference its diagrams"
            )
            for reference in referenced_images:
                assert (repository_root / "docs" / reference).resolve() in assets
        else:
            assert resolved.is_file(), resolved
            installed_parent = PureWindowsPath(entry["DestDir"])
            installed_relative = Path(*installed_parent.parts[1:]) / resolved.name
            assert installed_relative == resolved.relative_to(repository_root)


def test_installer_keeps_operational_tools_and_excludes_moved_development_payload():
    server_entry = next(
        entry
        for entry in _installer_entries("Files")
        if entry["DestDir"] == r"{app}\server"
    )
    patterns = server_entry["Excludes"].replace("\\", "/").split(",")
    for relative in (
        "setup/validation.py",
        "setup/tools/init_runtime.py",
        "setup/tools/generate_secrets.py",
        "setup/tools/generate_dev_cert.py",
        "setup/tools/enable_object_processing.py",
        "tools/prepare_osnet.py",
        "tools/export_release_manifest.py",
        "tools/requirements-osnet.txt",
        "tools/README.md",
        "services/data/tools/backup_database.py",
        "services/external/tools/bootstrap_admin.py",
    ):
        assert Path("server", relative).is_file()
        assert not any(fnmatchcase(relative, pattern) for pattern in patterns)
    for relative in (
        "setup/install_helper/cli.py",
        "setup/install_helper/tests/test_install_helper.py",
        "setup/install_helper/.venv/Scripts/python.exe",
        "setup/tests/test_config_core.py",
        "services/external/tools/export_openapi.py",
        "secrets/data.env",
        "secrets/data.env.bak",
        "secrets/camera_credentials.json.bak",
        "secrets/old/custom.backup",
        ".env.bak",
        "compose.env.bak",
        "config/config.yaml.bak",
        "config/config.yaml",
        "runtime/database/cctv.db",
    ):
        assert any(fnmatchcase(relative, pattern) for pattern in patterns), relative


def test_installer_secrets_allowlist_contains_only_the_five_public_templates():
    entries = _installer_entries("Files")
    selected = [
        entry for entry in entries if entry["DestDir"] == r"{app}\server\secrets"
    ]
    assert {PureWindowsPath(entry["Source"]).name for entry in selected} == {
        f"{name}.env.example"
        for name in ("data", "external", "preprocessing", "media", "analysis")
    }
    assert len(selected) == 5
    assert all("*" not in entry["Source"] for entry in selected)


# 업그레이드 정리 규칙은 알려진 옛 소스만 대상으로 하며 와일드카드나 운영 저장소를 포함하지 않는다.
def test_upgrade_removes_only_known_obsolete_sources_and_leaves_user_data():
    installer = (PACKAGING / "AI_CCTV_Server.iss").read_text(encoding="utf-8")
    entries = _installer_entries("InstallDelete")
    names = {entry["Name"] for entry in entries}
    assert r"{app}\server\install_helper\README.md" in names
    for filename in (
        "init_runtime.py",
        "generate_secrets.py",
        "generate_dev_cert.py",
        "enable_object_processing.py",
        "doctor.py",
        "bootstrap_admin.py",
        "backup_database.py",
    ):
        assert rf"{{app}}\server\scripts\{filename}" in names
    for name in names:
        assert "*" not in name
        assert "{commonappdata}" not in name
        assert not any(
            part in name for part in ("\\runtime", "\\secrets", "\\config\\")
        )
    assert "Type: filesandordirs" not in installer
    assert 'Type: dirifempty; Name: "{app}\\server\\scripts"' in installer
