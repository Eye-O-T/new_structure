#!/bin/sh
# 녹화가 끝난 파일의 카메라·경로·길이를 Data에 등록한다. 영상 본문은 전송하지 않는다.
set -eu

: "${MTX_PATH:?MediaMTX did not provide MTX_PATH}"
: "${MTX_SEGMENT_PATH:?MediaMTX did not provide MTX_SEGMENT_PATH}"
: "${MTX_SEGMENT_DURATION:?MediaMTX did not provide MTX_SEGMENT_DURATION}"
DATA_API_TOKEN="${DATA_MEDIA_TOKEN:-${INTERNAL_SERVICE_TOKEN:-}}"
: "${DATA_API_TOKEN:?DATA_MEDIA_TOKEN or legacy INTERNAL_SERVICE_TOKEN is not configured}"

# 공개·내부 API와 같은 카메라 ID 규칙을 검사하여 경로 해석이 달라지지 않게 한다.
case "$MTX_PATH" in
  ""|*[!a-z0-9_-]*)
    echo "recording hook rejected invalid camera path" >&2
    exit 2
    ;;
esac

case "${MTX_PATH%${MTX_PATH#?}}" in
  [a-z0-9]) ;;
  *)
    echo "recording hook rejected camera path with an invalid first character" >&2
    exit 2
    ;;
esac

if [ "${#MTX_PATH}" -gt 64 ]; then
  echo "recording hook rejected camera path longer than 64 characters" >&2
  exit 2
fi

# MediaMTX 1.9는 단위 없는 소수 초를 전달한다. 변환하지 않고 검증 후 넘긴다.
if ! awk -v duration="$MTX_SEGMENT_DURATION" 'BEGIN {
  exit !(duration ~ /^[0-9]+([.][0-9]+)?$/ && duration + 0 > 0)
}'; then
  echo "recording hook rejected invalid segment duration" >&2
  exit 2
fi

# 일시 장애에는 제한된 횟수로 재시도한다. 영상 파일은 이미 저장됐으므로
# 알림이 끝내 실패해도 Data의 파일 정합성 점검이 나중에 등록을 보완할 수 있다.
curl \
  --fail \
  --silent \
  --show-error \
  --noproxy '*' \
  --connect-timeout 3 \
  --max-time 15 \
  --retry 4 \
  --retry-delay 1 \
  --retry-all-errors \
  --header "X-Internal-Token: ${DATA_API_TOKEN}" \
  --data-urlencode "camera_id=${MTX_PATH}" \
  --data-urlencode "segment_path=${MTX_SEGMENT_PATH}" \
  --data-urlencode "duration_seconds=${MTX_SEGMENT_DURATION}" \
  http://nginx:8080/internal/data/v1/hooks/recording-complete
