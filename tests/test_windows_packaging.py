from pathlib import Path


PACKAGING = Path("configurator/packaging")


def test_windows_packaging_sources_are_valid_utf8_without_replacement_text():
    for path in (
        PACKAGING / "AI_CCTV_Server.iss",
        Path("docs/operations/windows-installer.md"),
    ):
        text = path.read_text(encoding="utf-8")
        assert "\ufffd" not in text, path


def test_inno_installer_packages_gui_cli_and_required_compose_context():
    installer = (PACKAGING / "AI_CCTV_Server.iss").read_text(encoding="utf-8")

    assert 'Source: "..\\..\\dist\\AI_CCTV_Configurator.exe"' in installer
    assert 'Source: "..\\..\\dist\\AI_CCTV_CLI.exe"' in installer
    assert 'Source: "..\\..\\.dockerignore"' in installer
    assert 'Source: "..\\..\\src\\*"' in installer
    assert 'Source: "..\\..\\server\\*"' in installer
    assert "secrets\\*.env" in installer
    assert "secrets\\*.json" in installer
    assert "runtime\\*" in installer
    assert "config\\config.yaml" in installer
    assert "__pycache__\\*" in installer
    assert "*.key" in installer
    assert "*.pem" in installer
    assert "*.egg-info\\*" in installer


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


def test_inno_installer_has_consumer_entrypoints_and_safe_uninstall():
    installer = (PACKAGING / "AI_CCTV_Server.iss").read_text(encoding="utf-8")

    assert "AI CCTV Configurator" in installer
    assert "AI CCTV CLI Console" in installer
    assert "addtopath" in installer
    assert "AddInstallDirToPath" in installer
    assert "RemoveInstallDirFromPath" in installer
    assert "[UninstallRun]" in installer
    assert "AI_CCTV_CLI.exe" in installer
    assert "--env-file" in installer
    assert " down -v" not in installer


def test_windows_build_script_builds_both_entrypoints_and_checksum():
    script = (PACKAGING / "build_windows_installer.ps1").read_text(
        encoding="utf-8"
    )

    assert "ai_cctv_configurator.spec" in script
    assert "ai_cctv_cli.spec" in script
    assert "AI_CCTV_Configurator.exe" in script
    assert "AI_CCTV_CLI.exe" in script
    assert "ISCC.exe" in script
    assert "Get-FileHash" in script
    assert "SHA256" in script
    assert "$env:OS -ne 'Windows_NT'" in script
    assert "$IsWindows" not in script


def test_pyinstaller_specs_resolve_sources_from_the_repository_root():
    for name, entrypoint in (
        ("ai_cctv_configurator.spec", '"gui_entry.py"'),
        ("ai_cctv_cli.spec", '"cli_entry.py"'),
    ):
        spec = (PACKAGING / name).read_text(encoding="utf-8")
        assert "Path(SPECPATH).resolve().parents[1]" in spec
        assert "str(repository_root" in spec
        assert entrypoint in spec
