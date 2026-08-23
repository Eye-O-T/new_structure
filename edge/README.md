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
sudo ai-cctv-edge export-auth-token --output /home/pi/edge-001-control.token
# 중앙 Configurator에서 Edge를 등록하고 cam-001-publish.json을 받은 뒤:
sudo ai-cctv-edge setup --publish-credentials-file /home/pi/cam-001-publish.json
sudo ai-cctv-edge doctor
sudo ai-cctv-edge status
```

추론 모델은 Edge에 두지 않는다. 사용자가 내려받은 호환 모델 파일은 중앙 서버
Configurator에서 로컬 경로로 선택한다.
