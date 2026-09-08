# Argon2로 비밀번호를 해시하거나 검증한다. 저장된 해시에서 원래 비밀번호를 복원하지 않는다.
from argon2 import PasswordHasher

_PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19_456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)


def hash_password(password: str) -> str:
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return bool(_PASSWORD_HASHER.verify(password_hash, password))
    except Exception:
        return False
