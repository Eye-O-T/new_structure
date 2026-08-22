"""Generate a short-lived self-signed TLS certificate for local testing."""

from __future__ import annotations

import argparse
import ipaddress
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def parse_args() -> argparse.Namespace:
    server_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=server_dir / "runtime" / "certificates",
    )
    parser.add_argument("--hostname", default="localhost")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def subject_alt_name(hostname: str) -> str:
    values = ["DNS:localhost", "IP:127.0.0.1"]
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        entry = f"DNS:{hostname}"
    else:
        entry = f"IP:{hostname}"
    if entry not in values:
        values.append(entry)
    return ",".join(values)


def main() -> int:
    args = parse_args()
    if args.days < 1 or args.days > 397:
        raise SystemExit("--days must be between 1 and 397")

    openssl = shutil.which("openssl")
    if openssl is None:
        raise SystemExit("OpenSSL was not found on PATH")

    output_dir = args.output_dir.expanduser().resolve()
    certificate = output_dir / "tls.crt"
    private_key = output_dir / "tls.key"
    if not args.force and (certificate.exists() or private_key.exists()):
        raise SystemExit(
            f"refusing to overwrite an existing certificate in {output_dir}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output_dir) as temporary_directory:
        temporary_root = Path(temporary_directory)
        temporary_certificate = temporary_root / "tls.crt"
        temporary_key = temporary_root / "tls.key"
        command = [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:3072",
            "-sha256",
            "-nodes",
            "-days",
            str(args.days),
            "-subj",
            f"/CN={args.hostname}",
            "-addext",
            f"subjectAltName={subject_alt_name(args.hostname)}",
            "-keyout",
            str(temporary_key),
            "-out",
            str(temporary_certificate),
        ]
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise SystemExit(
                result.stderr.strip() or "OpenSSL certificate generation failed"
            )
        os.chmod(temporary_key, 0o600)
        os.replace(temporary_key, private_key)
        os.replace(temporary_certificate, certificate)

    print(f"[OK] generated local-only certificate: {certificate}")
    print("[WARN] replace it with a trusted certificate before external access")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
