#!/bin/sh
set -eu

if [ "$(uname -m)" != "aarch64" ]; then
    echo "Build this package on ARM64 or in an ARM64 build container." >&2
    exit 1
fi
if [ -z "${MEDIAMTX_BINARY:-}" ] || [ -z "${MEDIAMTX_SHA256:-}" ]; then
    echo "Set MEDIAMTX_BINARY and verified MEDIAMTX_SHA256." >&2
    exit 1
fi
printf '%s  %s\n' "$MEDIAMTX_SHA256" "$MEDIAMTX_BINARY" | sha256sum -c -

repo_root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
package_root=$(mktemp -d)
trap 'rm -rf "$package_root"' EXIT INT TERM
root="$package_root/ai-cctv-edge_0.3.0_arm64"

install -d "$root/DEBIAN" "$root/etc/ai-cctv-edge" \
    "$root/usr/lib/ai-cctv-edge/wheels" "$root/usr/bin" \
    "$root/lib/systemd/system"
cp "$repo_root/edge/packaging/debian/"* "$root/DEBIAN/"
cp "$repo_root/edge/config/config.example.toml" \
    "$root/etc/ai-cctv-edge/config.toml"
cp "$repo_root/edge/systemd/ai-cctv-edge.service" "$root/lib/systemd/system/"
cp "$MEDIAMTX_BINARY" "$root/usr/lib/ai-cctv-edge/mediamtx"
chmod 0755 "$root/usr/lib/ai-cctv-edge/mediamtx" "$root/DEBIAN/postinst" "$root/DEBIAN/prerm"

python3 -m pip wheel "$repo_root/edge" --wheel-dir "$root/usr/lib/ai-cctv-edge/wheels"
printf '%s\n' '#!/bin/sh' \
    'exec /usr/lib/ai-cctv-edge/venv/bin/ai-cctv-edge "$@"' \
    > "$root/usr/bin/ai-cctv-edge"
chmod 0755 "$root/usr/bin/ai-cctv-edge"
dpkg-deb --build --root-owner-group "$root" "$repo_root/ai-cctv-edge_0.3.0_arm64.deb"
