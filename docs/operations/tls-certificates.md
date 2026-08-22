# TLS 인증서 운영

## 로컬 개발

다음 명령은 기본 30일 self-signed certificate를
`server/runtime/certificates/tls.crt`와 `tls.key`에 만든다.

```bash
python server/scripts/generate_dev_cert.py
```

다른 LAN hostname을 시험할 때는 `--hostname`을 지정한다. 개발 인증서는
브라우저와 일반 client가 신뢰하지 않으며 외부 배포용이 아니다.

## 운영 배포

운영 인증서는 신뢰 CA에서 발급받고 다음 조건을 확인한다.

1. 접속 DNS name이 SAN에 포함된다.
2. full-chain certificate를 `tls.crt`, private key를 `tls.key`로 배치한다.
3. private key는 관리자와 Docker runtime만 읽을 수 있게 보호한다.
4. `CERTS_DIR`을 해당 Host directory로 설정한다.
5. `nginx -t`와 실제 client의 인증서 검증을 통과한다.
6. 만료 전 자동 또는 운영 절차로 갱신하고 Nginx를 reload한다.

```bash
docker compose --env-file server/.env -f server/compose.yml exec -T nginx nginx -t
docker compose --env-file server/.env -f server/compose.yml exec -T nginx nginx -s reload
```

인증서 자동 발급 방식은 배포 환경의 DNS와 port-forwarding 정책에 따라 달라서
이 저장소의 최초 Compose 기준에서는 자동으로 고정하지 않는다.
