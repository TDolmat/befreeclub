"""Testy szyfrowania Fernet (app/core/secret_box.py).

Bez DB - czysta kryptografia + zachowanie przy braku/zlym master key.
Po kazdej zmianie SECRETS_MASTER_KEY wolamy reset_cache(), bo Fernet jest
lazy singletonem per proces.
"""

import pytest
from cryptography.fernet import Fernet

from app.core import secret_box
from app.core.config import settings as env


@pytest.fixture(autouse=True)
def _reset_box():
    secret_box.reset_cache()
    yield
    secret_box.reset_cache()


@pytest.fixture
def with_key(monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setattr(env, "SECRETS_MASTER_KEY", key)
    secret_box.reset_cache()
    return key


def test_is_available_with_key(with_key):
    assert secret_box.is_available() is True


def test_round_trip(with_key):
    token = secret_box.encrypt("sk-tajny-klucz-123")
    assert secret_box.decrypt(token) == "sk-tajny-klucz-123"


def test_ciphertext_differs_from_plaintext(with_key):
    plaintext = "resend_api_key_value"
    token = secret_box.encrypt(plaintext)
    assert token != plaintext
    assert plaintext not in token


def test_decrypt_corrupted_token_returns_none(with_key):
    token = secret_box.encrypt("wartosc")
    corrupted = token[:-4] + "XXXX"
    assert secret_box.decrypt(corrupted) is None


def test_decrypt_garbage_returns_none(with_key):
    assert secret_box.decrypt("to-nie-jest-token-fernet") is None


def test_decrypt_foreign_token_returns_none(with_key, monkeypatch):
    # Token zaszyfrowany INNYM kluczem - po podmianie master key decrypt = None.
    other = Fernet(Fernet.generate_key())
    foreign = other.encrypt(b"obca-wartosc").decode("utf-8")
    assert secret_box.decrypt(foreign) is None


def test_no_master_key_not_available(monkeypatch):
    monkeypatch.setattr(env, "SECRETS_MASTER_KEY", None)
    secret_box.reset_cache()
    assert secret_box.is_available() is False


def test_no_master_key_encrypt_raises(monkeypatch):
    monkeypatch.setattr(env, "SECRETS_MASTER_KEY", None)
    secret_box.reset_cache()
    with pytest.raises(secret_box.SecretBoxUnavailable):
        secret_box.encrypt("cokolwiek")


def test_no_master_key_decrypt_returns_none(monkeypatch):
    monkeypatch.setattr(env, "SECRETS_MASTER_KEY", None)
    secret_box.reset_cache()
    assert secret_box.decrypt("cokolwiek") is None


def test_bad_master_key_not_available(monkeypatch):
    monkeypatch.setattr(env, "SECRETS_MASTER_KEY", "to-nie-jest-poprawny-klucz-fernet")
    secret_box.reset_cache()
    assert secret_box.is_available() is False
    with pytest.raises(secret_box.SecretBoxUnavailable):
        secret_box.encrypt("x")
    assert secret_box.decrypt("x") is None
