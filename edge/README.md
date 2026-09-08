# Raspberry Pi Edge

카메라 영상을 H.264로 중앙 MediaMTX에 보내며 로컬에도 계속 기록한다. 중앙 연결 장애 중에도 로컬 백업을 유지한다. 대상은 Raspberry Pi OS Bookworm ARM64다. AI 모델은 중앙에서 실행한다.

[설치·Pairing·업데이트](../README.md#edge-연결)와 [패키지 빌드](../README.md#패키지-빌드)를 따른다.

| 위치 | 역할 |
|---|---|
| `src/ai_cctv_edge/` | 카메라 송출·설정·제어·복구 |
| `tests/` | Edge 기능·배포 계약 테스트 |
| `packaging/` | ARM64 패키지·서비스 설치 |

공통 테스트 실행은 [개발 안내](../README.md#개발과-검증)를 참고한다.

## Edge 코드 검증

Raspberry Pi 또는 Python 3.11 개발 환경에서 저장소 루트 기준으로 실행한다. 실제 카메라·GStreamer 검증은 Pi에서 따로 수행한다.

```sh
python -m pip install -e './edge[test]'
python -m pytest -c edge/pyproject.toml edge/tests -q
```

서버와의 프로토콜 연동은 서버 테스트 컨테이너에서도 확인한다. Edge의 OS 패키지·서비스 설치는 `.deb` 패키지가 담당한다.
