# ADR-0002: MediaMTX 1.9.0 고정과 Alpine wrapper

- 상태: Accepted for the first integration baseline
- 날짜: 2026-08-22

## 목차

- [결정](#결정)
- [결과](#결과)

## 결정

검증 기준과 동일한 `bluenviron/mediamtx:1.9.0`을 사용한다. 공식 image는
scratch image이므로 녹화 완료 Hook과 HTTP Health Check에 필요한 shell,
CA certificate, curl을 포함한 작은 Alpine wrapper image를 빌드한다.

MediaMTX 설정은 RTSP(TCP), HLS(fMP4), Recording(fMP4), Playback, Control
API만 활성화한다. RTMP, WebRTC, SRT, Metrics와 PPROF는 비활성화한다.

## 결과

- Release 전에 upstream과 Alpine image digest를 release manifest에 기록한다.
- MediaMTX를 변경할 때 RTSP publish/read, HLS, 60초 녹화, Playback,
  Control API, publish 인증과 recording hook을 모두 회귀 시험한다.
- Hook 실패는 MediaMTX 녹화를 취소하지 않는다. Data reconciliation이 누락된
  metadata를 보상해야 한다.
