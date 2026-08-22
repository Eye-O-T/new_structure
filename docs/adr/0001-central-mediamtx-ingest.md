# ADR-0001: 중앙 MediaMTX 수집 경계

- 상태: Accepted for the Compose baseline
- 날짜: 2026-08-22

## 결정

중앙 서버는 하나의 MediaMTX를 실행하고, Camera ID와 동일한 RTSP path에
H.264 publisher를 수용한다. 기준 Compose는 모든 유효한 path를 publisher
source로 열되, publish 요청은 External Service의 MediaMTX HTTP 인증
endpoint가 Camera ID별 자격 증명을 확인한다.

초기 Edge 마이그레이션에서 central-pull을 시험할 수 있으나, source URL과
credential은 Control API를 통해 개별 path에 설정해야 한다. 이 시험 때문에
중앙 MediaMTX 외에 새 미디어 게이트웨이를 추가하지 않는다.

## 근거

- 카메라별 HLS, 녹화와 Playback을 중앙 한 지점에서 제공한다.
- 다른 카메라를 중단하지 않고 path별 등록과 해제가 가능하다.
- 인터넷에는 RTSP를 공개하지 않고 신뢰 LAN 주소에만 bind할 수 있다.

## 결과

- Edge는 `rtsp://<central-lan-ip>:8554/<camera_id>`에 게시한다.
- Camera ID는 `^[a-z0-9][a-z0-9_-]{0,63}$`를 따른다.
- 동일 path의 publisher 교체는 허용하지 않는다.
- RTSP publish credential과 LAN 방화벽이 모두 필요하다.
