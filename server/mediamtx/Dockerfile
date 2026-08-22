ARG MEDIAMTX_UPSTREAM_IMAGE=bluenviron/mediamtx:1.9.0
FROM ${MEDIAMTX_UPSTREAM_IMAGE} AS upstream

ARG MEDIAMTX_WRAPPER_BASE_IMAGE=alpine:3.20.3
FROM ${MEDIAMTX_WRAPPER_BASE_IMAGE}

RUN apk add --no-cache ca-certificates curl

COPY --from=upstream /mediamtx /usr/local/bin/mediamtx
COPY recording-complete-hook.sh /usr/local/bin/recording-complete-hook

RUN chmod 0755 /usr/local/bin/mediamtx /usr/local/bin/recording-complete-hook

EXPOSE 8554 8888 9996 9997
STOPSIGNAL SIGINT
ENTRYPOINT ["/usr/local/bin/mediamtx"]
CMD ["/etc/mediamtx.yml"]
