# 제어·복구 HTTP API에서 사용하는 Bearer 토큰을 읽고 요청마다 검증한다.
from __future__ import annotations

import hmac
from pathlib import Path

from fastapi import Header, HTTPException


def load_tokens(*paths: Path) -> tuple[str, ...]:
    tokens: list[str] = []
    for path in dict.fromkeys(paths):
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if len(token) >= 32 and token not in tokens:
            tokens.append(token)
    if not tokens:
        raise RuntimeError("edge auth token must contain at least 32 characters")
    return tuple(tokens)


class BearerAuthenticator:
    def __init__(self, tokens: tuple[str, ...]):
        self.tokens = tokens

    def __call__(self, authorization: str | None = Header(default=None)) -> None:
        supplied = ""
        if authorization:
            scheme, separator, credentials = authorization.partition(" ")
            if separator and scheme.lower() == "bearer":
                supplied = credentials
        # 일반 문자열 비교 대신 비교 시간에 따른 토큰 추측을 줄이는 함수를 사용한다.
        if not any(hmac.compare_digest(supplied, token) for token in self.tokens):
            raise HTTPException(
                status_code=401,
                detail="invalid edge auth token",
                headers={"WWW-Authenticate": "Bearer"},
            )
