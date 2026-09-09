import hashlib
import json

from server.tools.export_release_manifest import collect


def test_release_record_contains_actual_images_packages_and_models_without_secrets(
    tmp_path,
):
    env = tmp_path / "compose.env"
    env.write_text("DATA_EXTERNAL_TOKEN=private-token")
    model = tmp_path / "detector.pt"
    model.write_bytes(b"fixture-model")
    commands = []

    def runner(command):
        commands.append(command)
        if "compose" in command:
            return json.dumps(
                [
                    {"Service": "preprocessing", "ID": "pre", "State": "running"},
                    {"Service": "analysis", "ID": "ana", "State": "running"},
                ]
            )
        if command[1] == "inspect":
            assert command[2:4] == ["--format", "{{.Image}}"]
            return "sha256:actual-image"
        if command[1:3] == ["image", "inspect"]:
            return json.dumps(
                [
                    {
                        "Id": "sha256:actual-image",
                        "RepoDigests": ["image@sha256:registry"],
                        "Config": {"Env": ["SECRET=private-token"]},
                    }
                ]
            )
        assert command[1:3] == ["exec", "pre"]
        return json.dumps([{"name": "transitive-dependency", "version": "1.2.3"}])

    result = collect(tmp_path, env, [model], runner)
    assert (
        result["services"]["preprocessing"]["python_packages"][0]["version"] == "1.2.3"
    )
    assert result["models"][0]["sha256"] == hashlib.sha256(b"fixture-model").hexdigest()
    assert result["services"]["analysis"]["image_id"] == "sha256:actual-image"
    assert not any(command[1:3] == ["exec", "ana"] for command in commands)
    assert "private-token" not in json.dumps(result)
    assert result["missing_services"] == ["data", "external", "mediamtx", "nginx"]
