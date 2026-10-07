# AI CCTV User Guide

This guide follows the current implementation for Server, Edge cameras, and Mobile.

## First installation

Use the matching Server installer, Edge ARM64 `.deb`, and signed Android APK from a GitHub Release when those assets exist. Otherwise use the source path:

```powershell
git clone <repository>
cd new_structure
python --version
uv --version
docker --version
docker compose version
uv sync --project server/setup/install_helper --locked
uv run --project server/setup/install_helper --locked python -m server.setup.install_helper
```

The Install Helper performs preflight checks, collects deployment settings, writes deployment files, and can start services. The administrator password is 4–12 characters inclusive.

For CLI help:

```powershell
uv run --project server/setup/install_helper --locked python -m server.setup.install_helper.cli --help
```

## CPU and GPU

CPU-only is the default:

```text
PREPROCESSING_GPU=false
INFERENCE_DEVICE=auto
IDENTITY_DEVICE=auto
```

GPU mode requires an NVIDIA driver and Docker GPU support:

```text
PREPROCESSING_GPU=true
INFERENCE_DEVICE=auto
IDENTITY_DEVICE=auto
```

`auto` uses CUDA when available and otherwise CPU. `cpu` forces CPU. `cuda` or `cuda:N` requires the requested CUDA device and never silently falls back to CPU. Runtime status exposes the actual device/provider.

YOLO and OSNet model files are required when their corresponding AI features are enabled. HTTPS requires a certificate and private key; HTTP is supported when explicitly selected for a trusted LAN.

## Normal operating flow

1. Start the Server and wait for health/status to become ready.
2. Open Server Desktop and sign in.
3. Discover or manually register an Edge. Manual registration requires device ID, MAC address, management URL, recovery URL, auth token, camera ID, and camera name.
4. Select the camera and verify live video.
5. Use the live-view Bounding Box toggle for a UI overlay. The source RTSP/recording is not burn-in annotated.
6. Open an event to inspect Snapshot, Person Crop, annotated media, and related recording when available.
7. Install the Mobile APK, enter the Server URL and credentials, then verify camera/live/event media access.

## Edge and camera notes

Edge identity uses device ID and normalized MAC address. IP and management/recovery URL changes do not change identity. MAC is identification data, not an authentication secret; control/recovery authentication remains token based.

An external USB camera path may require FFmpeg. This is optional and is not a universal Server preflight requirement.

## Troubleshooting

- Run the Install Helper doctor for deployment and runtime checks.
- In CPU mode, missing NVIDIA tooling is not an error.
- In GPU mode, verify Docker GPU visibility and preprocessing status device/provider.
- If live video fails, verify Edge reachability, RTSP publish configuration, and MediaMTX status.
- Event media 404 means media may not exist or may have been retained; 403 indicates authorization failure.
