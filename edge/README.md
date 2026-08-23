# AI_CCTV Raspberry Pi Edge

Raspberry Pi Edge는 Camera 영상을 H.264로 취득해 중앙 MediaMTX에 게시하고, 중앙
장애 중에는 제한된 MPEG-TS 백업을 유지한다. 소비자 배포 대상은 Raspberry Pi OS
Bookworm 64-bit이며 설치 파일은 `ai-cctv-edge_<version>_arm64.deb`다.

설치, 중앙–Edge 자격증명 교환, GUI 없이 사용하는 CLI, 업그레이드·제거와 Release
빌드·검증 절차는 [Edge 설치·배포 가이드](../docs/operations/edge-deployment.md)를
따른다.

빠른 설치 명령:

```bash
sha256sum -c ai-cctv-edge_0.3.0_arm64.deb.sha256
sudo apt install ./ai-cctv-edge_0.3.0_arm64.deb
sudo ai-cctv-edge pair --device-id edge-001 --camera-id cam-001 --set-pairing-key
# 중앙 Configurator에서 같은 Key 입력 → Discover Edge → Register Edge and camera
sudo ai-cctv-edge doctor
sudo ai-cctv-edge status
```

`pair`는 설정 전 상태에서만 UDP 37020으로 서명된 광고를 보내고 관리 Port 8003에
임시 Pairing API를 연다. Configurator가 Camera를 등록하고 게시 자격증명을 전달하면
설정을 원자 저장한 뒤 Pairing을 종료하고 Capture/Control/Recovery Service를 시작한다.
Broadcast가 차단되면 `export-auth-token`과 `setup --publish-credentials-file`의 기존
수동 Handoff 절차를 사용한다.

추론 모델은 Edge에 두지 않는다. 사용자가 내려받은 호환 모델 파일은 중앙 서버
Configurator에서 로컬 경로로 선택한다.
