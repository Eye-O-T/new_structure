# ADR-0003: 보호된 HLS와 Playback 전달

- 상태: Accepted for the Compose baseline
- 날짜: 2026-08-22

## 결정

실시간 영상은 일반 fMP4 HLS로 제공한다. 외부 요청은 Nginx의
`/hls/`와 `/playback/`만 사용하고, 각 요청은 External Service의
`/internal/auth/verify`를 `auth_request`로 통과해야 한다. Web UI에는
HttpOnly Secure Cookie를 우선하고 API client는 Bearer token을 사용할 수 있다.

MediaMTX Playback 원본 endpoint는 다음과 같이 매핑한다.

- `/playback/list?path=cam-001` -> MediaMTX `/list?path=cam-001`
- `/playback/get?path=cam-001&start=...&duration=...&format=fmp4`
  -> MediaMTX `/get?...`

## 결과

- HLS와 Playback 원본 port는 Host에 publish하지 않는다.
- Query token은 기본 인증 수단으로 사용하지 않는다.
- 저장 영상 HLS VOD playlist 생성은 이번 기준에 포함하지 않는다.
