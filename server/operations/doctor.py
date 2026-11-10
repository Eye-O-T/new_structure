"""공통 배포 검사와 선택적인 설정·실행 상태 진단을 제공한다."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from server.deployment.validation import (
    Check,
    deployment_path,
    read_deployment_env,
    validate_deployment,
)

from .compose import ComposeAdapter, default_server_dir


def _compose_rows(raw: str) -> list[dict]:
    """Compose의 JSON 배열과 줄별 JSON 출력을 같은 목록으로 정규화한다."""

    if not isinstance(raw, str):
        raise ValueError("Compose status output is unavailable")
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
        from server.installer.models import (
            ANALYSIS_PLUGIN,
            ANALYSIS_PLUGIN_ALIASES,
            IDENTITY_PLUGIN,
            deployed_analysis_model,
            deployed_identity_model,
            validate_analysis_model,
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
        analysis_plugin = environment.get("ANALYSIS_PLUGIN") or ANALYSIS_PLUGIN
        if analysis_plugin not in ANALYSIS_PLUGIN_ALIASES:
            results.append(
                Check(
                    "ERROR",
                    "Analysis plugin",
                    "ANALYSIS_PLUGIN must select the PAR person attribute analyzer",
                )
            )
        else:
            analysis_model = deployed_analysis_model(environment, models_root)
            analysis_ready = False
            if analysis_model is not None:
                try:
                    validate_analysis_model(analysis_model)
                except (OSError, ValueError):
                    pass
                else:
                    analysis_ready = True
            results.append(
                Check(
                    "OK" if analysis_ready else "WARN",
                    "PAR attribute model",
                    str(analysis_model)
                    if analysis_ready
                    else "PAR attribute bundle (par_serving_exp3.pt) is missing or invalid inside /models; configured analysis worker cannot load it",
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
    if payload.get("inference_enabled") is False:
        results.append(Check("WARN", "Object inference", "disabled by runtime configuration"))
    elif payload.get("inference_enabled") is True and payload.get("worker_count") == 0:
        results.append(Check("WARN", "Object inference", "enabled, but no active camera workers"))
    workers = payload.get("workers", {})
    if not isinstance(workers, dict) or any(
        not isinstance(worker, dict) for worker in workers.values()
    ):
        raise ValueError("invalid camera status response")
    if payload.get("gpu_requested") is True:
        ready_workers = [worker for worker in workers.values() if worker.get("model_ready")]
        if not workers:
            results.append(Check("WARN", "Preprocessing GPU", "GPU is requested; no active camera workers, so detection execution is unverified"))
        elif not ready_workers:
            results.append(Check("WARN", "Preprocessing GPU", "GPU is requested; no detection model is ready"))
        elif any(
            worker.get("requested_device") != "cpu"
            and not str(worker.get("execution_device", "")).startswith("cuda")
            for worker in ready_workers
        ):
            results.append(Check("WARN", "Preprocessing GPU", "GPU is requested, but a ready detector is running on CPU"))
        else:
            results.append(Check("OK", "Preprocessing GPU", "ready detection workers are using CUDA"))
    for camera_id, worker in sorted(workers.items()):
        online = worker.get("state") == "online"
        model_ready = worker.get("model_ready") is True
        fresh = worker.get("frame_stale") is False
        delivery_error = bool(worker.get("event_delivery_error"))
        live_delivery_failures = worker.get("live_objects_consecutive_failures", 0)
        results.append(
            Check(
                "OK"
                if online and model_ready and fresh and not delivery_error
                and not (type(live_delivery_failures) is int and live_delivery_failures >= 3)
                else "WARN",
                f"Camera {camera_id}",
                f"online={online}, model_ready={model_ready}, frame_fresh={fresh}, "
                f"event_delivery_error={delivery_error}, "
                f"live_objects_delivery_failures={live_delivery_failures}, "
                f"detections={worker.get('detection_object_count', 'unknown')}",
            )
        )
    identity = payload.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    identity_last_error = identity.get("last_error")
    identity_device_error = isinstance(identity_last_error, str) and identity_last_error in {
        "IDENTITY_DEVICE_UNAVAILABLE",
        "IDENTITY_DEVICE_EXECUTION_FAILED",
    }
    if payload.get("gpu_requested") is True and identity_device_error:
        results.append(
            Check(
                "ERROR", "Identity GPU", identity_last_error
            )
        )
    elif (
        payload.get("gpu_requested") is True
        and identity.get("model_ready") is True
        and identity.get("requested_device") != "cpu"
    ):
        identity_gpu = str(identity.get("execution_device", "")).startswith("cuda")
        results.append(Check(
            "OK" if identity_gpu else "ERROR",
            "Identity GPU",
            "identity worker is using CUDA" if identity_gpu else "GPU is requested, but identity is running on CPU",
        ))
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
    if ps.returncode != 0:
        results.append(
            Check(
                "ERROR",
                "Compose services",
                (ps.stderr or "").strip() or "status query failed",
            )
        )
        return results
    try:
        rows = _compose_rows(ps.stdout)
    except ValueError as exc:
        results.append(Check("ERROR", "Compose services", str(exc)))
        return results
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
    environment = read_deployment_env(adapter.env_file)
    if environment.get("PREPROCESSING_GPU", "false").lower() in {"true", "1", "yes"}:
        container_result = adapter.run("ps", "-q", "preprocessing", capture=True)
        container_id = (container_result.stdout or "").strip().splitlines()
        if container_result.returncode != 0 or not container_id:
            results.append(Check("WARN", "Preprocessing GPU runtime", "container allocation could not be inspected"))
        else:
            allocation = subprocess.run(
                ["docker", "inspect", "--format", "{{json .HostConfig.DeviceRequests}}", container_id[0]],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            requests_gpu = False
            if allocation.returncode == 0:
                try:
                    requests_gpu = bool(json.loads(allocation.stdout.strip()))
                except ValueError:
                    pass
            runtime = adapter.run(
                "exec", "-T", "preprocessing", "python", "-c",
                "import json,torch; a=torch.cuda.is_available(); "
                "print(json.dumps({'available':a,'count':torch.cuda.device_count(),"
                "'device':torch.cuda.get_device_name(0) if a else None}))",
                capture=True,
            )
            try:
                device = json.loads(runtime.stdout.strip()) if runtime.returncode == 0 else {}
            except ValueError:
                device = {}
            ready = requests_gpu and device.get("available") is True and device.get("count", 0) > 0
            detail = (
                f"DeviceRequests={requests_gpu}, CUDA available={device.get('available', False)}, "
                f"device={device.get('device') or 'unavailable'}"
            )
            results.append(
                Check("OK" if ready else "ERROR", "Preprocessing GPU runtime", detail)
            )
        analysis_result = adapter.run("ps", "-q", "analysis", capture=True)
        analysis_ids = (analysis_result.stdout or "").strip().splitlines()
        if analysis_result.returncode != 0 or not analysis_ids:
            results.append(
                Check(
                    "WARN", "Analysis GPU runtime",
                    "container allocation could not be inspected",
                )
            )
        else:
            allocation = subprocess.run(
                ["docker", "inspect", "--format", "{{json .HostConfig.DeviceRequests}}", analysis_ids[0]],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            requests_gpu = False
            if allocation.returncode == 0:
                try:
                    requests_gpu = bool(json.loads(allocation.stdout.strip()))
                except ValueError:
                    pass
            runtime = adapter.run(
                "exec", "-T", "analysis", "python", "-c",
                "import json,torch; a=torch.cuda.is_available(); print(json.dumps({'available':a,'count':torch.cuda.device_count()}))",
                capture=True,
            )
            try:
                device = json.loads(runtime.stdout.strip()) if runtime.returncode == 0 else {}
            except ValueError:
                device = {}
            probe = adapter.run(
                "exec", "-T", "analysis", "python", "-c",
                (
                    "import json,urllib.request,urllib.error;"
                    "op=urllib.request.build_opener(urllib.request.ProxyHandler({}));"
                    "u='http://127.0.0.1:8000/health/ready';"
                    "\ntry:\n d=json.load(op.open(u,timeout=3))\n"
                    "except urllib.error.HTTPError as e:\n"
                    " d=json.load(e) if e.headers.get('Content-Type','').startswith('application/json') else {}\n"
                    "except (urllib.error.URLError,ValueError,OSError):\n d={}\n"
                    "print(json.dumps(d))"
                ),
                capture=True,
            )
            try:
                status = json.loads(probe.stdout.strip()) if probe.returncode == 0 else {}
                status = status.get("detail", status)
                if not isinstance(status, dict):
                    status = {}
            except ValueError:
                status = {}
            expected_gpu = environment.get("ANALYSIS_DEVICE", "auto").lower() != "cpu"
            gpu_ready = (
                requests_gpu
                and device.get("available") is True
                and device.get("count", 0) > 0
                and status.get("ready") is True
                and status.get("model_ready") is True
                and (
                    not expected_gpu
                    or str(status.get("execution_device", "")).startswith("cuda")
                )
            )
            results.append(
                Check(
                    "OK" if gpu_ready else "ERROR",
                    "Analysis GPU runtime",
                    f"DeviceRequests={requests_gpu}, CUDA available={device.get('available', False)}, "
                    f"model_ready={status.get('model_ready', False)}, "
                    f"requested={status.get('requested_device', 'unknown')}, "
                    f"execution={status.get('execution_device', 'unknown')}",
                )
            )
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
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                    check=False,
                )
                message = (
                    (result.stdout or result.stderr or "").strip()
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
    environment = read_deployment_env(adapter.env_file)
    if environment.get("PREPROCESSING_GPU", "false").lower() in {"true", "1", "yes"}:
        gpu_compose = adapter.server_dir / "compose.gpu.yml"
        results.append(
            Check(
                "OK" if gpu_compose.is_file() else "ERROR",
                "GPU Compose override",
                str(gpu_compose) if gpu_compose.is_file() else "compose.gpu.yml is missing",
            )
        )
        try:
            config = adapter.run("--profile", "ai", "config", "--format", "json", capture=True)
            config_payload = json.loads(config.stdout) if config.returncode == 0 else {}
            services = config_payload.get("services", {})
            preprocessing = services.get("preprocessing", {})
            analysis = services.get("analysis", {})
            gpu_configured = bool(preprocessing.get("gpus")) and bool(analysis.get("gpus"))
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            gpu_configured = False
        results.append(
            Check(
                "OK" if gpu_configured else "ERROR",
                "GPU container request",
                "resolved Compose requests Docker GPU access for preprocessing and analysis" if gpu_configured else "resolved Compose GPU request is missing from preprocessing or analysis",
            )
        )
        results.append(
            Check(
                "OK" if shutil.which("nvidia-smi") else "WARN",
                "NVIDIA host tooling",
                "nvidia-smi available" if shutil.which("nvidia-smi") else "nvidia-smi not found; GPU smoke test unavailable",
            )
        )
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
