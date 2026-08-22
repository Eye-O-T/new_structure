from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(?:pt|onnx|engine)$")
MODEL_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_MODEL_BYTES = 2 * 1024**3


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install_from_manifest(manifest_path: Path, models_root: Path) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    required = {"name", "version", "url", "sha256", "license"}
    missing = required - set(manifest)
    if missing:
        raise ValueError(f"model manifest missing: {', '.join(sorted(missing))}")
    if not manifest["url"] or not manifest["sha256"]:
        raise ValueError("model distribution is not configured in this release")
    name = str(manifest["name"])
    version = str(manifest["version"])
    checksum = str(manifest["sha256"])
    license_name = str(manifest["license"]).strip()
    if MODEL_NAME.fullmatch(name) is None:
        raise ValueError("model manifest name is invalid")
    if MODEL_VERSION.fullmatch(version) is None:
        raise ValueError("model manifest version is invalid")
    if SHA256.fullmatch(checksum) is None:
        raise ValueError("model manifest SHA-256 is invalid")
    if not license_name or license_name == "UNRESOLVED":
        raise ValueError("model manifest license is not approved")
    parsed_url = urlsplit(str(manifest["url"]))
    if parsed_url.scheme != "https" or not parsed_url.hostname:
        raise ValueError("model manifest URL must use HTTPS")
    if parsed_url.username or parsed_url.password or parsed_url.fragment:
        raise ValueError("model manifest URL contains unsupported components")

    target_dir = models_root.resolve() / version
    target = target_dir / name
    target_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=target_dir, prefix=".download-")
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        request = Request(str(manifest["url"]), headers={"User-Agent": "AI_CCTV/0.3.0"})
        with urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            final_url = urlsplit(response.geturl())
            if final_url.scheme != "https" or not final_url.hostname:
                raise ValueError("model download redirected outside HTTPS")
            if final_url.username or final_url.password or final_url.fragment:
                raise ValueError("model download redirected to an unsupported URL")
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > MAX_MODEL_BYTES:
                raise ValueError("model download exceeds the size limit")
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_MODEL_BYTES:
                    raise ValueError("model download exceeds the size limit")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        actual = sha256_file(temporary)
        if actual.lower() != checksum.lower():
            raise ValueError("downloaded model SHA-256 mismatch")
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def validate_custom_model(path: Path) -> None:
    if not path.is_file():
        raise ValueError("model file does not exist")
    if path.suffix.lower() not in {".pt", ".onnx", ".engine"}:
        raise ValueError("supported model formats are .pt, .onnx and .engine")
    with path.open("rb") as handle:
        if not handle.read(1):
            raise ValueError("model file is empty")
