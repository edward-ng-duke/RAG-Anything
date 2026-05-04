import bcrypt

BCRYPT_COST = 12


def hash_password(plain: str) -> str:
    if not plain or not isinstance(plain, str):
        raise ValueError("password must be non-empty string")
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_COST)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    if not isinstance(plain, str) or not isinstance(hashed, str):
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False
