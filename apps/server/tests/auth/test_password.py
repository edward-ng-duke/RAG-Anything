import pytest

from rag_service.auth.password import hash_password, verify_password


def test_hash_then_verify_succeeds():
    pwd = "correct horse battery staple"
    hashed = hash_password(pwd)
    assert isinstance(hashed, str)
    assert hashed != pwd
    assert verify_password(pwd, hashed) is True


def test_verify_wrong_password_fails():
    hashed = hash_password("right-password")
    assert verify_password("wrong-password", hashed) is False


def test_verify_invalid_hash_returns_false():
    # Not a valid bcrypt hash; should not raise.
    assert verify_password("anything", "not-a-bcrypt-hash") is False
    assert verify_password("anything", "") is False


def test_hash_empty_raises():
    with pytest.raises(ValueError):
        hash_password("")


def test_hash_non_string_raises():
    with pytest.raises(ValueError):
        hash_password(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        hash_password(12345)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        hash_password(b"bytes-not-str")  # type: ignore[arg-type]


def test_two_hashes_of_same_pwd_differ():
    pwd = "same-password"
    h1 = hash_password(pwd)
    h2 = hash_password(pwd)
    assert h1 != h2  # different salts
    assert verify_password(pwd, h1) is True
    assert verify_password(pwd, h2) is True
