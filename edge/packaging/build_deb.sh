#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
edge_root="$repo_root/edge"
output_dir=${OUTPUT_DIR:-"$repo_root/dist/edge"}
expected_mediamtx_version=${MEDIAMTX_VERSION:-v1.9.0}

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

for command in awk basename chmod dpkg-deb find install mktemp rm sha256sum \
    python3 sed touch tr uname; do
    command -v "$command" >/dev/null 2>&1 || fail "required command not found: $command"
done
python3 -m pip --version >/dev/null 2>&1 || fail "python3 pip module is required"

[ "$(uname -m)" = "aarch64" ] || \
    fail "build on Raspberry Pi OS ARM64 or another trusted aarch64 builder"
[ -n "${MEDIAMTX_BINARY:-}" ] || fail "set MEDIAMTX_BINARY"
[ -f "$MEDIAMTX_BINARY" ] || fail "MEDIAMTX_BINARY is not a regular file"
[ -n "${MEDIAMTX_SHA256:-}" ] || fail "set the independently verified MEDIAMTX_SHA256"
[ "${#MEDIAMTX_SHA256}" -eq 64 ] || fail "MEDIAMTX_SHA256 must contain 64 hexadecimal characters"
case "$MEDIAMTX_SHA256" in
    *[!0-9A-Fa-f]*) fail "MEDIAMTX_SHA256 must be hexadecimal" ;;
esac

actual_mediamtx_sha256=$(sha256sum "$MEDIAMTX_BINARY" | awk '{print $1}')
[ "$(printf '%s' "$actual_mediamtx_sha256" | tr 'A-F' 'a-f')" = \
  "$(printf '%s' "$MEDIAMTX_SHA256" | tr 'A-F' 'a-f')" ] || \
    fail "MediaMTX SHA-256 does not match MEDIAMTX_SHA256"

package_version=$(sed -n \
    '/^\[project\]$/,/^\[/{s/^version = "\([^"]*\)"$/\1/p;}' \
    "$edge_root/pyproject.toml")
[ -n "$package_version" ] || fail "could not read the Edge version from pyproject.toml"
[ "$(sed -n 's/^Version: //p' "$edge_root/packaging/debian/control")" = \
  "$package_version" ] || fail "Debian control version does not match pyproject.toml"

if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
    if command -v git >/dev/null 2>&1; then
        SOURCE_DATE_EPOCH=$(git -C "$repo_root" log -1 --format=%ct 2>/dev/null || true)
    fi
fi
SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-}
case "$SOURCE_DATE_EPOCH" in
    ''|*[!0-9]*) fail "set SOURCE_DATE_EPOCH to a valid source timestamp" ;;
esac
[ "$SOURCE_DATE_EPOCH" -ge 315532800 ] || \
    fail "SOURCE_DATE_EPOCH must be on or after 1980-01-01 for wheel archives"
export SOURCE_DATE_EPOCH
export LC_ALL=C
export TZ=UTC
export PYTHONHASHSEED=0
export PIP_DISABLE_PIP_VERSION_CHECK=1
umask 022

package_root=$(mktemp -d)
trap 'rm -rf "$package_root"' EXIT INT TERM
root="$package_root/ai-cctv-edge_${package_version}_arm64"
artifact="$output_dir/ai-cctv-edge_${package_version}_arm64.deb"

install -d "$root/DEBIAN" "$root/etc/ai-cctv-edge" \
    "$root/usr/lib/ai-cctv-edge/wheels" "$root/usr/bin" \
    "$root/lib/systemd/system" "$root/usr/share/doc/ai-cctv-edge" \
    "$output_dir"
install -m 0644 "$edge_root/packaging/debian/control" "$root/DEBIAN/control"
install -m 0644 "$edge_root/packaging/debian/conffiles" "$root/DEBIAN/conffiles"
install -m 0755 "$edge_root/packaging/debian/postinst" "$root/DEBIAN/postinst"
install -m 0755 "$edge_root/packaging/debian/prerm" "$root/DEBIAN/prerm"
install -m 0644 "$edge_root/config/config.example.toml" \
    "$root/etc/ai-cctv-edge/config.toml"
for unit in "$edge_root/systemd/"*.service; do
    install -m 0644 "$unit" "$root/lib/systemd/system/$(basename "$unit")"
done
install -m 0755 "$MEDIAMTX_BINARY" "$root/usr/lib/ai-cctv-edge/mediamtx"

mediamtx_version_output=$(
    "$root/usr/lib/ai-cctv-edge/mediamtx" --version 2>&1 || true
)
case "$mediamtx_version_output" in
    *"$expected_mediamtx_version"*) ;;
    *) fail "MediaMTX reports '$mediamtx_version_output'; expected $expected_mediamtx_version" ;;
esac

python3 -m pip wheel \
    --constraint "$edge_root/packaging/constraints.txt" \
    --wheel-dir "$root/usr/lib/ai-cctv-edge/wheels" \
    "$edge_root"
printf '%s\n' '#!/bin/sh' \
    'exec /usr/lib/ai-cctv-edge/venv/bin/ai-cctv-edge "$@"' \
    > "$root/usr/bin/ai-cctv-edge"
chmod 0755 "$root/usr/bin/ai-cctv-edge"

source_revision=unknown
source_state=unknown
if command -v git >/dev/null 2>&1; then
    source_revision=$(git -C "$repo_root" rev-parse HEAD 2>/dev/null || true)
    source_revision=${source_revision:-unknown}
    if git -C "$repo_root" diff --quiet --ignore-submodules HEAD -- 2>/dev/null && \
       [ -z "$(git -C "$repo_root" ls-files --others --exclude-standard 2>/dev/null)" ]; then
        source_state=clean
    else
        source_state=dirty
    fi
fi
{
    printf 'package=ai-cctv-edge\n'
    printf 'version=%s\n' "$package_version"
    printf 'architecture=arm64\n'
    printf 'source_revision=%s\n' "$source_revision"
    printf 'source_state=%s\n' "$source_state"
    printf 'source_date_epoch=%s\n' "$SOURCE_DATE_EPOCH"
    printf 'mediamtx_version=%s\n' "$expected_mediamtx_version"
    printf 'mediamtx_sha256=%s\n' "$actual_mediamtx_sha256"
} > "$root/usr/share/doc/ai-cctv-edge/build-info"

# Normalize all payload timestamps. dpkg-deb also consumes SOURCE_DATE_EPOCH for
# the archive headers, making repeated builds comparable when every input wheel
# and the MediaMTX binary are identical.
find "$root" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +
rm -f "$artifact" "$artifact.sha256"
dpkg-deb --build --root-owner-group --uniform-compression -Zxz -z9 \
    "$root" "$artifact"
sh "$edge_root/packaging/verify_deb.sh" "$artifact" "$actual_mediamtx_sha256"
(
    cd "$output_dir"
    sha256sum "$(basename "$artifact")" > "$(basename "$artifact").sha256"
)
printf 'Built: %s\n' "$artifact"
printf 'Checksum: %s.sha256\n' "$artifact"
