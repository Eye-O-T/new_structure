# 로컬 개발에서 HTTPS 연결을 시험할 자체 서명 인증서를 만든다.
# 운영 단말이 신뢰하는 CA 인증서를 자동 발급하는 도구는 아니다.


from __future__ import annotations

import argparse
import ipaddress
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


# 개발 인증서의 출력 위치·호스트·유효기간과 기존 파일 덮어쓰기 여부를 읽는다.
def parse_args() -> argparse.Namespace:
    server_dir = Path(__file__).resolve().parents[2]
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


# localhost와 루프백은 항상 포함하고 추가 호스트가 IP인지 DNS 이름인지 구분해 SAN을 만든다.
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


# 기존 인증서 보호와 OpenSSL 유무를 확인한 뒤 임시 폴더에서 키·인증서를 생성하고 순서대로 교체한다.
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
