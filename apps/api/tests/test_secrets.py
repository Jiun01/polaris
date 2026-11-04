"""Tests for polaris_api.services.secrets — PR-2 PyNaCl implementation."""

from __future__ import annotations

import os
import secrets as py_secrets
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from polaris_api.services.secrets import (
    KEY_BYTES,
    MAC_BYTES,
    NONCE_BYTES,
    VERSION,
    CryptoError,
    _get_aead,
    decrypt,
    encrypt,
    health_check,
)

# Deterministic 32-byte test key (hex) — different from any real KMS key.
_TEST_KEY_HEX = "00112233445566778899aabbccddeeff" "ffeeddccbbaa99887766554433221100"


@pytest.fixture(autouse=True)
def _set_test_kms_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("POLARIS_KMS_KEY", _TEST_KEY_HEX)
    # _get_aead caches the Aead instance built from the key. Tests that change
    # the env var (missing/malformed/short key) need a fresh build. Clear
    # before + after each test for safety.
    _get_aead.cache_clear()
    yield
    _get_aead.cache_clear()


def test_round_trip_recovers_plaintext() -> None:
    tenant = uuid4()
    plaintext = b"shhh - tenant credentials"
    ct = encrypt(plaintext, tenant)
    assert decrypt(ct, tenant) == plaintext


def test_round_trip_empty_plaintext() -> None:
    tenant = uuid4()
    ct = encrypt(b"", tenant)
    assert decrypt(ct, tenant) == b""


def test_ciphertext_carries_version_and_nonce_prefix() -> None:
    tenant = uuid4()
    ct = encrypt(b"x", tenant)
    assert ct[0] == VERSION
    assert len(ct) == 1 + NONCE_BYTES + 1 + MAC_BYTES  # version + nonce + 1B plaintext + tag


def test_two_encrypts_of_same_plaintext_differ() -> None:
    """Nonce is random — same plaintext must produce different ciphertexts."""
    tenant = uuid4()
    pt = b"identical"
    assert encrypt(pt, tenant) != encrypt(pt, tenant)


def test_tampered_ciphertext_raises() -> None:
    tenant = uuid4()
    ct = bytearray(encrypt(b"sensitive", tenant))
    # Flip a bit in the ciphertext body (after version + nonce).
    ct[-1] ^= 0x01
    with pytest.raises(CryptoError):
        decrypt(bytes(ct), tenant)


def test_wrong_tenant_aad_raises() -> None:
    tenant_a = uuid4()
    tenant_b = uuid4()
    ct = encrypt(b"sensitive", tenant_a)
    with pytest.raises(CryptoError):
        decrypt(ct, tenant_b)


def test_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POLARIS_KMS_KEY", raising=False)
    with pytest.raises(CryptoError, match="not set"):
        encrypt(b"x", uuid4())


def test_malformed_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLARIS_KMS_KEY", "not-hex-xx")
    with pytest.raises(CryptoError, match="invalid"):
        encrypt(b"x", uuid4())


def test_short_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # 16 bytes hex = 32 chars
    monkeypatch.setenv("POLARIS_KMS_KEY", py_secrets.token_hex(16))
    with pytest.raises(CryptoError, match="invalid"):
        encrypt(b"x", uuid4())


def test_unsupported_version_raises() -> None:
    tenant = uuid4()
    ct = bytearray(encrypt(b"x", tenant))
    ct[0] = 0xFF
    with pytest.raises(CryptoError, match="version"):
        decrypt(bytes(ct), tenant)


def test_truncated_ciphertext_raises() -> None:
    with pytest.raises(CryptoError, match="too short"):
        decrypt(b"\x01" + b"\x00" * 10, uuid4())


def test_non_bytes_plaintext_raises() -> None:
    with pytest.raises(CryptoError, match="bytes"):
        encrypt("not bytes", uuid4())  # type: ignore[arg-type]


def test_health_check_true_with_valid_key() -> None:
    assert health_check() is True


def test_health_check_false_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POLARIS_KMS_KEY", raising=False)
    assert health_check() is False


def test_fixed_tenant_uuid_round_trip() -> None:
    """Use a fixed UUID — protects against accidental random AAD pollution."""
    tenant = UUID("12345678-1234-5678-1234-567812345678")
    pt = b"deterministic-test"
    ct = encrypt(pt, tenant)
    assert decrypt(ct, tenant) == pt


def test_key_constant_matches_pynacl_aead() -> None:
    """Guard against future drift if PyNaCl changes XChaCha20 sizes."""
    from nacl.secret import Aead

    assert KEY_BYTES == Aead.KEY_SIZE
    assert NONCE_BYTES == Aead.NONCE_SIZE
    assert MAC_BYTES == Aead.MACBYTES


def test_kms_key_env_unchanged_after_calls() -> None:
    """Defensive: encrypt() must not mutate POLARIS_KMS_KEY."""
    before = os.environ["POLARIS_KMS_KEY"]
    encrypt(b"x", uuid4())
    assert os.environ["POLARIS_KMS_KEY"] == before
