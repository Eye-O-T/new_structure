ADMIN_PASSWORD_MIN_LENGTH = 4
ADMIN_PASSWORD_MAX_LENGTH = 12


def validate_admin_password(password: str) -> None:
    if not ADMIN_PASSWORD_MIN_LENGTH <= len(password) <= ADMIN_PASSWORD_MAX_LENGTH:
        raise ValueError("administrator password must contain 4 to 12 characters")
