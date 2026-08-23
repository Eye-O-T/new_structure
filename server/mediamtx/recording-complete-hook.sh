#!/bin/sh
set -eu

: "${MTX_PATH:?MediaMTX did not provide MTX_PATH}"
: "${MTX_SEGMENT_PATH:?MediaMTX did not provide MTX_SEGMENT_PATH}"
: "${MTX_SEGMENT_DURATION:?MediaMTX did not provide MTX_SEGMENT_DURATION}"
DATA_API_TOKEN="${DATA_MEDIA_TOKEN:-${INTERNAL_SERVICE_TOKEN:-}}"
: "${DATA_API_TOKEN:?DATA_MEDIA_TOKEN or legacy INTERNAL_SERVICE_TOKEN is not configured}"

# Keep the same Camera ID contract as the public and internal APIs.
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

# MediaMTX v1.9 emits a Go duration (for example 1m0.125s). Convert it to
# numeric seconds because the Data API field is named duration_seconds.
duration_seconds="$({
  awk -v duration="$MTX_SEGMENT_DURATION" '
    BEGIN {
      total = 0
      rest = duration

      hpos = index(rest, "h")
      if (hpos > 0) {
        total += substr(rest, 1, hpos - 1) * 3600
        rest = substr(rest, hpos + 1)
      }

      mpos = index(rest, "m")
      if (mpos > 0 && substr(rest, mpos + 1, 1) != "s") {
        total += substr(rest, 1, mpos - 1) * 60
        rest = substr(rest, mpos + 1)
      }

      if (rest ~ /^[0-9]+([.][0-9]+)?ms$/) {
        total += substr(rest, 1, length(rest) - 2) / 1000
        rest = ""
      } else if (rest ~ /^[0-9]+([.][0-9]+)?us$/) {
        total += substr(rest, 1, length(rest) - 2) / 1000000
        rest = ""
      } else if (rest ~ /^[0-9]+([.][0-9]+)?ns$/) {
        total += substr(rest, 1, length(rest) - 2) / 1000000000
        rest = ""
      } else if (rest ~ /^[0-9]+([.][0-9]+)?s$/) {
        total += substr(rest, 1, length(rest) - 1)
        rest = ""
      }

      if (rest != "") {
        exit 1
      }

      printf "%.9f", total
    }
  '
} || true)"

if [ -z "$duration_seconds" ]; then
  echo "recording hook could not parse segment duration" >&2
  exit 2
fi

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
  --data-urlencode "duration_seconds=${duration_seconds}" \
  http://nginx:8080/internal/data/v1/hooks/recording-complete
