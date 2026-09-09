"""Preprocessing과 Data가 공유하는 미전송 관측 보호 목록의 용량 계약."""

MANIFEST_NAME = ".pending-observations.json"
MANIFEST_SCHEMA_VERSION = 2
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_PROTECTED_PATHS = 300_000
MAX_PROTECTED_EVENTS = 100_000
