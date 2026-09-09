# Argon2로 비밀번호를 해시하거나 검증한다. 저장된 해시에서 원래 비밀번호를 복원하지 않는다.
from argon2 import PasswordHasher

_PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19_456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)


# Argon2가 생성한 개별 salt를 포함하는 저장용 비밀번호 해시를 반환한다.
def hash_password(password: str) -> str:
    return _PASSWORD_HASHER.hash(password)


# 불일치뿐 아니라 손상된 해시도 인증 실패로 처리하여 예외가 인증 경계를 넘지 않게 한다.
def verify_password(password_hash: str, password: str) -> bool:
    try:
        return bool(_PASSWORD_HASHER.verify(password_hash, password))
    except Exception:
        return False
