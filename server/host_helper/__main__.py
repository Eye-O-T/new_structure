"""Structured command interface for host-side Edge discovery and pairing."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Any

from server.edge_onboarding.discovery import DiscoveredEdge, discover_edges
from server.edge_onboarding.pairing import complete_edge_pairing, probe_edge_connection
from server.edge_onboarding.server_api import redact_for_display


def _edge(value: Any) -> DiscoveredEdge:
    if not isinstance(value, dict):
        raise ValueError("edge must be a JSON object")
    fields = {
        "device_id", "camera_id", "address", "management_url", "recovery_url",
        "supported_profiles", "message_id", "sent_at", "mac_address", "advertised_ip",
    }
    if set(value) != fields:
        raise ValueError("edge fields do not match the discovery contract")
    value = dict(value)
    value["supported_profiles"] = tuple(value["supported_profiles"])
    return DiscoveredEdge(**value)


def _payload() -> dict[str, Any]:
    value = json.load(sys.stdin)
    if not isinstance(value, dict):
        raise ValueError("input must be a JSON object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-cctv-host-helper")
    sub = parser.add_subparsers(dest="command", required=True)
    discover = sub.add_parser("discover")
    discover.add_argument("--timeout", type=float, default=3.0)
    probe = sub.add_parser("probe")
    probe.add_argument("--timeout", type=float, default=5.0)
    pair = sub.add_parser("pair")
    pair.add_argument("--timeout", type=float, default=10.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "discover":
            result: Any = [asdict(item) for item in discover_edges(timeout=args.timeout)]
        elif args.command == "probe":
            value = _payload()
            result = probe_edge_connection(_edge(value["edge"]), timeout=args.timeout)
        else:
            value = _payload()
            result = complete_edge_pairing(
                _edge(value["edge"]),
                server_response=value["server_response"],
                operational_token=str(value["operational_token"]),
                central_host=str(value["central_host"]),
                central_port=int(value["central_port"]),
                video_profile=str(value["video_profile"]),
                backup_root=str(value["backup_root"]),
                timeout=args.timeout,
            )
        print(json.dumps(redact_for_display(result), ensure_ascii=False))
        return 0
    except (KeyError, TypeError, ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        message = "Edge pairing failed" if args.command == "pair" else str(exc)
        print(
            json.dumps(
                {"error": {"code": "HOST_HELPER_FAILED", "message": message}},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
