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
        if not any(hmac.compare_digest(supplied, token) for token in self.tokens):
            raise HTTPException(
                status_code=401,
                detail="invalid edge auth token",
                headers={"WWW-Authenticate": "Bearer"},
            )
