"""내부 계약(DB 스키마, 배포·설정 스키마, 도메인 계약)의 SHA-256 서명 검증 및 초기화."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_cctv_core.config import AppConfig
from ai_cctv_core.contracts.objects import (
    IdentityDescriptor,
    LiveObject,
    LiveObjects,
    ObjectJobCompletion,
    ObjectObservation,
)

from .private_files import restrict_private_file
from .validation import ALLOWED_SECRET_KEYS

SIGNATURE_SCHEMA_VERSION = 1
SIGNATURE_FILENAME = "contract-signature.json"
PROJECT_SIGNATURE_FILENAME = ".contract-signature.json"

_COMPONENT_LABELS = {
    "db_schema": "데이터베이스 마이그레이션 스키마 (migrations/*.sql)",
    "deployment_contract": "배포/설정/시크릿 계약 (AppConfig, compose.yml, 시크릿 키)",
    "domain_contract": "서비스 간 도메인 데이터 계약 (ai_cctv_core.contracts)",
    "signature_file": "계약 해시 서명 파일 형식",
}

_PRESERVED_DIR_NAMES = {"models", "certs", "certificates"}

_ALLOWED_SECRET_KEYS = {
    service: sorted(keys) for service, keys in ALLOWED_SECRET_KEYS.items()
}

_REQUIRED_DEPLOYMENT_ENV_KEYS = [
    "ANALYSIS_SECRETS_FILE",
    "CONFIG_FILE",
    "DATABASE_DIR",
    "DATA_SECRETS_FILE",
    "EXTERNAL_SECRETS_FILE",
    "MEDIA_SECRETS_FILE",
    "PREPROCESSING_SECRETS_FILE",
]


@dataclass(frozen=True)
class ContractSignature:
    schema_version: int
    combined_sha256: str
    components: dict[str, str]
    updated_at: str = ""

    def to_dict(self, *, data_root: Path | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "combined_sha256": self.combined_sha256,
            "components": dict(sorted(self.components.items())),
            "updated_at": self.updated_at or datetime.now(timezone.utc).isoformat(),
        }
        if data_root is not None:
            payload["data_root"] = str(data_root.expanduser().resolve())
        return payload


class ContractSignatureMismatchError(ValueError):
    """저장된 계약 해시와 현재 코드의 계약 해시가 다를 때 발생하는 예외."""

    def __init__(
        self,
        *,
        data_root: Path,
        server_dir: Path,
        changed_components: list[str],
        stored_hash: str | None,
        current_hash: str,
    ) -> None:
        self.data_root = data_root.expanduser().resolve()
        self.server_dir = server_dir.expanduser().resolve()
        self.changed_components = tuple(changed_components)
        self.stored_hash = stored_hash
        self.current_hash = current_hash
        labels = [
            _COMPONENT_LABELS.get(name, name) for name in self.changed_components
        ]
        changed_text = ", ".join(labels) if labels else "내부 시스템 계약"
        self.user_message = (
            "시스템 내부 계약 또는 코드 변경이 감지되었습니다.\n"
            f"- 변경된 영역: {changed_text}\n"
            "- 기존 저장 데이터(DB/설정/시크릿)를 초기화하고 새 설정값을 입력받습니다.\n"
            "- AI 모델(models/)과 TLS 인증서(certs/) 파일은 그대로 보존됩니다."
        )
        super().__init__(self.user_message)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_text(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip() + "\n"


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _canonical_server_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _compute_db_schema_hash(server_dir: Path) -> str:
    migrations_dir = (
        server_dir / "services" / "data" / "app" / "database" / "migrations"
    )
    if not migrations_dir.is_dir():
        migrations_dir = (
            _canonical_server_dir()
            / "services"
            / "data"
            / "app"
            / "database"
            / "migrations"
        )
    entries: list[dict[str, str]] = []
    if migrations_dir.is_dir():
        for sql_file in sorted(migrations_dir.glob("*.sql"), key=lambda p: p.name):
            normalized = _normalize_text(sql_file.read_text(encoding="utf-8"))
            entries.append(
                {
                    "name": sql_file.name,
                    "sha256": _sha256_bytes(normalized.encode("utf-8")),
                }
            )
    return _sha256_bytes(_canonical_json_bytes(entries))


def _compute_deployment_contract_hash(server_dir: Path) -> str:
    compose_path = server_dir / "compose.yml"
    if not compose_path.is_file():
        compose_path = _canonical_server_dir() / "compose.yml"
    compose_normalized = (
        _normalize_text(compose_path.read_text(encoding="utf-8"))
        if compose_path.is_file()
        else ""
    )
    payload = {
        "app_config_schema": AppConfig.model_json_schema(),
        "allowed_secret_keys": _ALLOWED_SECRET_KEYS,
        "required_deployment_env_keys": _REQUIRED_DEPLOYMENT_ENV_KEYS,
        "compose_sha256": _sha256_bytes(compose_normalized.encode("utf-8")),
    }
    return _sha256_bytes(_canonical_json_bytes(payload))


def _compute_domain_contract_hash() -> str:
    models = (
        ObjectObservation,
        IdentityDescriptor,
        ObjectJobCompletion,
        LiveObject,
        LiveObjects,
    )
    payload = {model.__name__: model.model_json_schema() for model in models}
    return _sha256_bytes(_canonical_json_bytes(payload))


def compute_contract_signature(server_dir: Path | None = None) -> ContractSignature:
    """현재 코드베이스의 DB·배포·도메인 계약 SHA-256 서명을 계산한다."""

    resolved_server = (
        server_dir.expanduser().resolve()
        if server_dir is not None
        else _canonical_server_dir()
    )
    components = {
        "db_schema": _compute_db_schema_hash(resolved_server),
        "deployment_contract": _compute_deployment_contract_hash(resolved_server),
        "domain_contract": _compute_domain_contract_hash(),
    }
    combined = _sha256_bytes(
        _canonical_json_bytes(
            {
                "schema_version": SIGNATURE_SCHEMA_VERSION,
                "components": components,
            }
        )
    )
    return ContractSignature(
        schema_version=SIGNATURE_SCHEMA_VERSION,
        combined_sha256=combined,
        components=components,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


def deployment_signature_path(data_root: Path) -> Path:
    return data_root.expanduser().resolve() / "config" / SIGNATURE_FILENAME


def project_signature_path(server_dir: Path) -> Path:
    return server_dir.expanduser().resolve() / PROJECT_SIGNATURE_FILENAME


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o640)
        except OSError:
            restrict_private_file(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _should_sync_project_signature(data_root: Path, server_dir: Path) -> bool:
    """기본 저장소이거나 별도 테스트용 server_dir일 때 프로젝트 서명 파일도 동기화한다."""

    from server.operations.compose import default_data_root

    resolved_root = data_root.expanduser().resolve()
    resolved_server = server_dir.expanduser().resolve()
    if resolved_root == default_data_root().expanduser().resolve():
        return True
    if resolved_server != _canonical_server_dir():
        return True
    return False


def write_contract_signature(
    data_root: Path,
    server_dir: Path,
    signature: ContractSignature | None = None,
    *,
    sync_project: bool | None = None,
) -> Path:
    """계산된 계약 해시를 배포 설정 폴더와(필요 시) 프로젝트 폴더에 저장한다."""

    resolved_root = data_root.expanduser().resolve()
    resolved_server = server_dir.expanduser().resolve()
    sig = signature or compute_contract_signature(resolved_server)
    target = deployment_signature_path(resolved_root)
    payload = sig.to_dict(data_root=resolved_root)
    _write_json_atomic(target, payload)

    if sync_project is None:
        sync_project = _should_sync_project_signature(resolved_root, resolved_server)
    if sync_project and resolved_server.is_dir():
        try:
            _write_json_atomic(project_signature_path(resolved_server), payload)
        except OSError:
            pass
    return target


def _read_signature_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"__corrupt__": True}
    if not isinstance(raw, dict):
        return {"__corrupt__": True}
    return raw


def _changed_components(
    stored: dict[str, Any], current: ContractSignature
) -> list[str]:
    if stored.get("__corrupt__"):
        return ["signature_file"]
    if stored.get("schema_version") != current.schema_version:
        return ["signature_file"]
    stored_components = stored.get("components")
    if not isinstance(stored_components, dict):
        return ["signature_file"]
    changed: list[str] = []
    for key, current_value in current.components.items():
        if stored_components.get(key) != current_value:
            changed.append(key)
    for key in stored_components:
        if key not in current.components and key not in changed:
            changed.append(key)
    if not changed and stored.get("combined_sha256") != current.combined_sha256:
        changed.append("signature_file")
    return changed


def check_existing_signature(
    data_root: Path, server_dir: Path
) -> ContractSignature:
    """이미 저장된 서명 파일이 있으면 현재 코드 해시와 비교하고, 불일치 시 예외를 발생시킨다."""

    resolved_root = data_root.expanduser().resolve()
    resolved_server = server_dir.expanduser().resolve()
    current = compute_contract_signature(resolved_server)

    deploy_sig_path = deployment_signature_path(resolved_root)
    proj_sig_path = project_signature_path(resolved_server)

    stored = _read_signature_file(deploy_sig_path)
    if stored is None and proj_sig_path.is_file():
        candidate = _read_signature_file(proj_sig_path)
        if candidate is not None:
            recorded_root = candidate.get("data_root")
            if recorded_root is None or (
                isinstance(recorded_root, str)
                and Path(recorded_root).expanduser().resolve() == resolved_root
            ):
                stored = candidate

    if stored is not None:
        changed = _changed_components(stored, current)
        if changed:
            raise ContractSignatureMismatchError(
                data_root=resolved_root,
                server_dir=resolved_server,
                changed_components=changed,
                stored_hash=str(stored.get("combined_sha256") or ""),
                current_hash=current.combined_sha256,
            )
    return current


def ensure_signature_file(
    data_root: Path,
    server_dir: Path,
    signature: ContractSignature | None = None,
) -> Path:
    """해시 서명 파일이 없으면 현재 해시로 생성하고, 있으면 유지(및 프로젝트 파일 동기화)한다."""

    resolved_root = data_root.expanduser().resolve()
    resolved_server = server_dir.expanduser().resolve()
    target = deployment_signature_path(resolved_root)
    sig = signature or check_existing_signature(resolved_root, resolved_server)
    if not target.is_file():
        return write_contract_signature(resolved_root, resolved_server, sig)
    if _should_sync_project_signature(resolved_root, resolved_server):
        proj_target = project_signature_path(resolved_server)
        if not proj_target.is_file():
            try:
                _write_json_atomic(proj_target, sig.to_dict(data_root=resolved_root))
            except OSError:
                pass
    return target


def reset_incompatible_deployment(
    data_root: Path,
    server_dir: Path,
    *,
    stop_services: bool = True,
) -> dict[str, list[str]]:
    """계약 불일치 시 실행 중인 서비스를 멈추고 모델·인증서를 제외한 저장 데이터를 초기화한다."""

    resolved_root = data_root.expanduser().resolve()
    resolved_server = server_dir.expanduser().resolve()

    if stop_services:
        try:
            from server.operations.local_camera import stop_publisher

            stop_publisher(resolved_root)
        except Exception:
            pass

        env_file = resolved_root / "config" / "compose.env"
        if env_file.is_file() and (resolved_server / "compose.yml").is_file():
            try:
                from server.operations.compose import STOP_ARGUMENTS, ComposeAdapter

                ComposeAdapter(resolved_server, env_file).run(
                    *STOP_ARGUMENTS, capture=True
                )
            except Exception:
                pass

    removed: list[str] = []
    preserved: list[str] = []

    if resolved_root.is_dir():
        for entry in sorted(resolved_root.iterdir(), key=lambda p: p.name):
            if entry.is_dir() and entry.name.lower() in _PRESERVED_DIR_NAMES:
                preserved.append(entry.name)
                continue
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink(missing_ok=True)
                removed.append(entry.name)
            except OSError:
                # 일부 파일이 잠겨 있으면 내부 파일이라도 최대한 비운다.
                if entry.is_dir():
                    for child in entry.rglob("*"):
                        if child.is_file() or child.is_symlink():
                            try:
                                child.unlink(missing_ok=True)
                            except OSError:
                                pass
                    removed.append(entry.name)

    proj_sig = project_signature_path(resolved_server)
    if proj_sig.is_file():
        candidate = _read_signature_file(proj_sig)
        recorded_root = (candidate or {}).get("data_root")
        if recorded_root is None or (
            isinstance(recorded_root, str)
            and Path(recorded_root).expanduser().resolve() == resolved_root
        ):
            try:
                proj_sig.unlink(missing_ok=True)
            except OSError:
                pass

    return {"removed": removed, "preserved": preserved}
