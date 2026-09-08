# 공식 이미지에서 실행 파일만 가져오고, 녹화 완료 알림에 필요한 셸·curl을 추가한다.
ARG MEDIAMTX_UPSTREAM_IMAGE=bluenviron/mediamtx:1.9.0
FROM ${MEDIAMTX_UPSTREAM_IMAGE} AS upstream

ARG MEDIAMTX_WRAPPER_BASE_IMAGE=alpine:3.20.3
FROM ${MEDIAMTX_WRAPPER_BASE_IMAGE}

RUN apk add --no-cache ca-certificates curl

COPY --from=upstream /mediamtx /usr/local/bin/mediamtx
# COPY의 기준은 Compose의 build.context인 이 mediamtx 폴더다.
COPY recording-complete-hook.sh /usr/local/bin/recording-complete-hook

RUN chmod 0755 /usr/local/bin/mediamtx /usr/local/bin/recording-complete-hook

EXPOSE 8554 8888 9996 9997
# 종료 신호를 전달하여 쓰던 녹화 구간을 정리할 기회를 준다.
STOPSIGNAL SIGINT
ENTRYPOINT ["/usr/local/bin/mediamtx"]
CMD ["/etc/mediamtx.yml"]
