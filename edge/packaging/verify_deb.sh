#!/bin/sh
set -eu

usage() {
    echo "Usage: sh edge/packaging/verify_deb.sh PACKAGE.deb [MEDIAMTX_SHA256]" >&2
    exit 2
}

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage
package=$1
expected_mediamtx_sha256=${2:-}
[ -f "$package" ] || fail "package not found: $package"

for command in awk dpkg-deb find grep sed sha256sum sh stat tr; do
    command -v "$command" >/dev/null 2>&1 || fail "required command not found: $command"
done

repo_root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
expected_version=$(sed -n \
    '/^\[project\]$/,/^\[/{s/^version = "\([^"]*\)"$/\1/p;}' \
    "$repo_root/edge/pyproject.toml")
[ "$(dpkg-deb -f "$package" Package)" = "ai-cctv-edge" ] || \
    fail "unexpected Debian package name"
[ "$(dpkg-deb -f "$package" Version)" = "$expected_version" ] || \
    fail "Debian version does not match edge/pyproject.toml"
[ "$(dpkg-deb -f "$package" Architecture)" = "arm64" ] || \
    fail "Debian architecture is not arm64"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT INT TERM
dpkg-deb -x "$package" "$work/root"
dpkg-deb -e "$package" "$work/control"

for path in \
    "$work/root/etc/ai-cctv-edge/config.toml" \
    "$work/root/usr/bin/ai-cctv-edge" \
    "$work/root/usr/lib/ai-cctv-edge/mediamtx" \
    "$work/root/usr/share/doc/ai-cctv-edge/build-info" \
    "$work/root/lib/systemd/system/ai-cctv-edge.service" \
    "$work/root/lib/systemd/system/ai-cctv-edge-control.service" \
    "$work/root/lib/systemd/system/ai-cctv-edge-recovery.service"; do
    [ -f "$path" ] || fail "required package payload is missing: $path"
done

[ "$(stat -c '%a' "$work/root/usr/bin/ai-cctv-edge")" = 755 ] || \
    fail "Edge CLI launcher is not mode 0755"
[ "$(stat -c '%a' "$work/root/usr/lib/ai-cctv-edge/mediamtx")" = 755 ] || \
    fail "MediaMTX is not mode 0755"
sh -n "$work/control/postinst"
sh -n "$work/control/prerm"
grep -Fxq /etc/ai-cctv-edge/config.toml "$work/control/conffiles" || \
    fail "config.toml is not declared as a conffile"

set -- "$work/root/usr/lib/ai-cctv-edge/wheels"/ai_cctv_edge-*.whl
[ "$#" -eq 1 ] && [ -f "$1" ] || fail "expected exactly one Edge application wheel"
if find "$work/root/usr/lib/ai-cctv-edge/wheels" -type f -name '*x86*' | grep -q .; then
    fail "x86 wheel found in ARM64 package"
fi
find "$work/root/usr/lib/ai-cctv-edge/wheels" -type f \
    -name 'pydantic_core-*aarch64.whl' | grep -q . || \
    fail "ARM64 pydantic-core wheel is missing"

for secret in recovery.token publish.password; do
    [ ! -e "$work/root/etc/ai-cctv-edge/$secret" ] || \
        fail "generated secret was embedded in the package: $secret"
done

if [ -n "$expected_mediamtx_sha256" ]; then
    actual=$(sha256sum "$work/root/usr/lib/ai-cctv-edge/mediamtx" | awk '{print $1}')
    [ "$actual" = "$(printf '%s' "$expected_mediamtx_sha256" | tr 'A-F' 'a-f')" ] || \
        fail "packaged MediaMTX SHA-256 does not match"
fi

grep -Fqx "version=$expected_version" \
    "$work/root/usr/share/doc/ai-cctv-edge/build-info" || \
    fail "build-info version is missing or incorrect"
echo "OK: verified ai-cctv-edge $expected_version arm64 package"
