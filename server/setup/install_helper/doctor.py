"""공통 배포 검사와 선택적인 설정·실행 상태 진단을 제공한다."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from server.setup.validation import (
    Check,
    deployment_path,
    read_deployment_env,
    validate_deployment,
)

from .compose_adapter import ComposeAdapter, default_server_dir


def _compose_rows(raw: str) -> list[dict]:
    """Compose의 JSON 배열과 줄별 JSON 출력을 같은 목록으로 정규화한다."""

    text = raw.strip()
    if not text:
        return []
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        rows = []
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows
    if isinstance(decoded, dict):
        return [decoded]
    if isinstance(decoded, list):
        return [row for row in decoded if isinstance(row, dict)]
    return []


def _configuration_checks(
    adapter: ComposeAdapter, config_path: Path | None
) -> list[Check]:
    """전체 진단에서만 설정 패키지를 불러와 YAML·모델·쓰기 권한을 확인한다."""

    results = []
    try:
        # --skip-runtime은 외부 Python 패키지가 없는 소스 배포에서도 실행된다.
        from ai_cctv_core.config import load_config

        environment = read_deployment_env(adapter.env_file)
        selected_config = (
            config_path.expanduser().resolve()
            if config_path is not None
            else deployment_path(
                adapter.server_dir,
                environment.get("CONFIG_FILE", "./config/config.yaml"),
            )
        )
        config = load_config(selected_config)
        results.append(Check("OK", "Configuration", f"schema={config.schema_version}"))
        runtime_uid = environment.get("AI_CCTV_UID")
        results.append(
            Check(
                "WARN" if runtime_uid == "0" else "OK",
                "Container runtime user",
                "UID 0 selected; configure a mapped non-root AI_CCTV_RUNTIME_UID"
                if runtime_uid == "0"
                else f"UID {runtime_uid or 'image default'}",
            )
        )
        recording = deployment_path(
            adapter.server_dir,
            environment.get("RECORDINGS_DIR", config.recording.root),
        )
        results.append(
            Check(
                "OK" if recording.exists() and os_access_write(recording) else "ERROR",
                "Recording storage",
                str(recording),
            )
        )
        models_root = deployment_path(
            adapter.server_dir, environment.get("MODELS_DIR", "./runtime/models")
        )
        model = models_root / environment.get(
            "MODEL_FILE", Path(config.inference.model_path).name
        )
        results.append(
            Check(
                "OK" if model.is_file() else "WARN",
                "Inference model",
                str(model)
                if model.is_file()
                else "model file is missing; configured detection cannot load it",
            )
        )
        from server.setup.model_manager import (
            IDENTITY_PLUGIN,
            deployed_identity_model,
            validate_identity_model,
        )

        if (environment.get("IDENTITY_PLUGIN") or IDENTITY_PLUGIN) == IDENTITY_PLUGIN:
            identity = deployed_identity_model(environment, models_root)
            ready = False
            if identity is not None:
                try:
                    validate_identity_model(identity)
                except (OSError, ValueError):
                    pass
                else:
                    ready = True
            results.append(
                Check(
                    "OK" if ready else "ERROR",
                    "OSNet identity model",
                    str(identity)
                    if ready
                    else "OSNet requires a non-empty ONNX up to 256 MiB inside /models; run server/tools/prepare_osnet.py --output <MODELS_DIR>/osnet_x0_25_msmt17.onnx",
                )
            )
    except Exception as exc:
        results.append(Check("ERROR", "Configuration", str(exc)))
    return results


# 필수 서비스 상태를 모은 뒤 Preprocessing 내부 상태를 조회해 카메라별 연결 상태를 덧붙인다.
def _preprocessing_checks(payload: dict) -> list[Check]:
    """HTTP 200과 추론 정상 동작을 구분하고 원시 오류·민감 입력은 출력하지 않는다."""
    results = []
    ready = payload.get("status") == "ready" and payload.get("data_ready") is True
    results.append(
        Check(
            "OK" if ready else "WARN",
            "Preprocessing readiness",
            "ready" if ready else "degraded or readiness could not be confirmed",
        )
    )
    workers = payload.get("workers", {})
    if not isinstance(workers, dict) or any(
        not isinstance(worker, dict) for worker in workers.values()
    ):
        raise ValueError("invalid camera status response")
    for camera_id, worker in sorted(workers.items()):
        online = worker.get("state") == "online"
        model_ready = worker.get("model_ready") is True
        fresh = worker.get("frame_stale") is False
        delivery_error = bool(worker.get("event_delivery_error"))
        results.append(
            Check(
                "OK"
                if online and model_ready and fresh and not delivery_error
                else "WARN",
                f"Camera {camera_id}",
                f"online={online}, model_ready={model_ready}, frame_fresh={fresh}, "
                f"event_delivery_error={delivery_error}",
            )
        )
    identity = payload.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    identity_ready = (
        identity.get("ready") is True
        and identity.get("model_ready") is True
        and identity.get("stalled") is False
        and not identity.get("last_error")
        and not identity.get("last_error_code")
    )
    results.append(
        Check(
            "OK" if identity_ready else "WARN",
            "Identity worker",
            "ready" if identity_ready else "not ready, stalled, or reporting an error",
        )
    )
    delivery = payload.get("event_delivery")
    delivery = delivery if isinstance(delivery, dict) else {}
    pending, rejected = delivery.get("pending"), delivery.get("rejected")
    valid_counts = all(
        type(value) is int and value >= 0 for value in (pending, rejected)
    )
    delivery_ok = (
        valid_counts
        and pending == 0
        and rejected == 0
        and not delivery.get("last_error")
        and not delivery.get("last_error_code")
    )
    results.append(
        Check(
            "OK" if delivery_ok else "WARN",
            "Detection event outbox",
            f"pending={pending}, rejected={rejected}"
            if valid_counts
            else "queue status could not be confirmed",
        )
    )
    return results


def _runtime_checks(adapter: ComposeAdapter) -> list[Check]:
    results = []
    try:
        ps = adapter.run("ps", "--format", "json", capture=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return [Check("ERROR", "Compose services", str(exc))]
    rows = _compose_rows(ps.stdout) if ps.returncode == 0 else []
    if ps.returncode != 0:
        results.append(
            Check(
                "ERROR", "Compose services", ps.stderr.strip() or "status query failed"
            )
        )
    by_service = {str(row.get("Service") or row.get("Name")): row for row in rows}
    for service in (
        "data",
        "external",
        "preprocessing",
        "analysis",
        "mediamtx",
        "nginx",
    ):
        row = by_service.get(service)
        state = str((row or {}).get("State", "stopped")).lower()
        health = str((row or {}).get("Health", "")).lower()
        ok = state == "running" and health in {"", "healthy"}
        starting = state == "running" and health == "starting"
        results.append(
            Check(
                "OK" if ok else "WARN" if starting else "ERROR",
                f"Service {service}",
                f"state={state}, health={health or 'n/a'}",
            )
        )
    if by_service.get("preprocessing") is None:
        return results
    try:
        camera_probe = adapter.run(
            "exec",
            "-T",
            "preprocessing",
            "python",
            "-c",
            (
                "import json,urllib.request,urllib.error;"
                "opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));"
                "state=json.load(opener.open('http://127.0.0.1:8000/internal/v1/status',timeout=3));"
                "\ntry:\n"
                " health=json.load(opener.open('http://127.0.0.1:8000/health/ready',timeout=3))\n"
                " state['status']=health.get('status','unknown')\n"
                "except (urllib.error.URLError,ValueError,OSError):\n"
                " state['status']='unavailable'\n"
                "print(json.dumps(state))"
            ),
            capture=True,
        )
        if camera_probe.returncode != 0:
            results.append(
                Check("WARN", "Camera status", "Preprocessing status query failed")
            )
            return results
        results.extend(_preprocessing_checks(json.loads(camera_probe.stdout)))
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        results.append(
            Check(
                "WARN",
                "Camera status",
                "Preprocessing status is unavailable or invalid",
            )
        )
    return results


def checks(
    server_dir: Path,
    config_path: Path | None,
    *,
    env_file: Path | None = None,
    skip_runtime: bool = False,
    skip_compose: bool = False,
) -> list[Check]:
    """기존 호출 규약을 유지하고 Docker가 없어도 파일·인증 검사를 반환한다."""

    adapter = ComposeAdapter(server_dir, env_file)
    results = validate_deployment(
        adapter.server_dir, env_file=adapter.env_file, config_path=config_path
    )
    if not skip_runtime:
        results.extend(_configuration_checks(adapter, config_path))
    if skip_compose:
        return results

    docker = shutil.which("docker")
    if docker is None:
        results.append(
            Check(
                "ERROR",
                "Docker Engine",
                "Docker was not found. Install/start Docker Desktop and retry.",
            )
        )
        return results
    if not skip_runtime:
        for arguments, name in (
            (["info"], "Docker Engine"),
            (["compose", "version"], "Docker Compose"),
        ):
            try:
                result = subprocess.run(
                    [docker, *arguments],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                message = (
                    (result.stdout or result.stderr).strip()
                    if name == "Docker Compose"
                    else "running"
                    if result.returncode == 0
                    else "Docker is installed but not running"
                )
                results.append(
                    Check("OK" if result.returncode == 0 else "ERROR", name, message)
                )
            except (OSError, subprocess.SubprocessError) as exc:
                results.append(Check("ERROR", name, str(exc)))
    if not adapter.env_file.is_file() or not adapter.compose_file.is_file():
        results.append(
            Check(
                "ERROR",
                "Compose files",
                "deployment environment or compose.yml missing",
            )
        )
        return results
    try:
        compose = adapter.run("config", "--quiet", capture=True)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        results.append(Check("ERROR", "Compose configuration", str(exc)))
        return results
    results.append(
        Check(
            "OK" if compose.returncode == 0 else "ERROR",
            "Compose configuration",
            "valid" if compose.returncode == 0 else compose.stderr.strip(),
        )
    )
    if compose.returncode == 0 and not skip_runtime:
        results.extend(_runtime_checks(adapter))
    return results


def os_access_write(path: Path) -> bool:
    return os.access(path, os.W_OK)


# 파일·Compose·런타임 진단 범위를 인수로 선택하고 ERROR가 하나라도 있으면 실패 코드로 종료한다.
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-dir", type=Path, default=default_server_dir())
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--config-file", type=Path)
    parser.add_argument(
        "--skip-runtime",
        action="store_true",
        help="Check deployment files and Compose configuration only",
    )
    parser.add_argument(
        "--skip-compose",
        action="store_true",
        help="Skip Docker/Compose commands; combine with --skip-runtime for standard-library-only checks",
    )
    args = parser.parse_args(argv)
    results = checks(
        args.server_dir,
        args.config_file,
        env_file=args.env_file or args.server_dir.expanduser().resolve() / ".env",
        skip_runtime=args.skip_runtime,
        skip_compose=args.skip_compose,
    )
    for result in results:
        print(f"[{result.status}] {result.name}: {result.message}")
    return 1 if any(result.status == "ERROR" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
