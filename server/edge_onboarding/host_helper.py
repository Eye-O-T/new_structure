"""Client for the short-lived Edge host helper process."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict
from typing import Any

from server.desktop.launcher import desktop_command

from .discovery import DiscoveredEdge


def run_host_helper(command: str, payload: dict[str, Any] | None = None, *, timeout: float) -> Any:
    options = {
        "input": json.dumps(payload, ensure_ascii=False) if payload is not None else None,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "timeout": timeout + 5,
        "check": False,
    }
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        desktop_command("host_helper", (command, "--timeout", str(timeout))),
        **options,
    )
    if result.returncode:
        try:
            error = json.loads(result.stderr).get("error", {}).get("message")
        except (AttributeError, json.JSONDecodeError):
            error = None
        raise RuntimeError(error or "Host Helper 작업에 실패했습니다.")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Host Helper가 올바른 JSON을 반환하지 않았습니다.") from exc


def discover_edges_via_helper(*, timeout: float = 3.0) -> list[DiscoveredEdge]:
    result = run_host_helper("discover", timeout=timeout)
    if not isinstance(result, list):
        raise RuntimeError("Host Helper 검색 결과가 올바르지 않습니다.")
    edges = []
    for item in result:
        item = dict(item)
        item["supported_profiles"] = tuple(item["supported_profiles"])
        edges.append(DiscoveredEdge(**item))
    return edges


def probe_edge_via_helper(edge: DiscoveredEdge, *, timeout: float = 5.0) -> dict[str, Any]:
    return run_host_helper("probe", {"edge": asdict(edge)}, timeout=timeout)


def pair_edge_via_helper(
    edge: DiscoveredEdge,
    *,
    server_response: dict[str, Any],
    operational_token: str,
    central_host: str,
    central_port: int,
    video_profile: str,
    backup_root: str,
    timeout: float = 10.0,
) -> dict[str, Any]:
    return run_host_helper(
        "pair",
        {
            "edge": asdict(edge),
            "server_response": server_response,
            "operational_token": operational_token,
            "central_host": central_host,
            "central_port": central_port,
            "video_profile": video_profile,
            "backup_root": backup_root,
        },
        timeout=timeout,
    )
