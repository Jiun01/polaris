"""Boot-time KMS key fingerprint check.

Why this exists
---------------
Regenerating `POLARIS_KMS_KEY` after PR-3 lands is catastrophic — every
`tenant_secrets` row becomes undecryptable because XChaCha20-Poly1305 has
no way to derive the old key from the new one. We can't undo it, can't
warn the operator at write time, can't even detect it at the per-row
decrypt path until a tenant request fails.

Solution: a one-row `platform_health` table holds an encrypted "sentinel"
— a fixed plaintext encrypted under the current key, plus the SHA-256 of
that plaintext. On every API boot:

1. Decrypt the sentinel ciphertext with whatever key the running process
   loaded from `POLARIS_KMS_KEY`.
2. SHA-256 the recovered plaintext.
3. Compare against the stored hash with a constant-time comparison.
4. Mismatch → abort startup. Match → log + continue.

If the operator rotates the key without re-seeding the sentinel, boot
fails LOUDLY before serving any traffic, instead of leaking `decrypt
failed` errors per tenant request.

Operational story
-----------------
- Fresh environment: run `apps/api/scripts/seed_kms_sentinel.py` once
  after `make migrate` to populate the platform_health row. Local dev
  may skip the seed; staging + prod refuse to boot without it (lifespan
  enforcement in main.py).
- Intentional key rotation: re-run the seed script with the new key set.
  This replaces the sentinel ciphertext + hash. All existing
  tenant_secrets rows must be re-encrypted under the new key in the same
  maintenance window — that's a separate migration script, deliberately
  out of scope for PR-3 (no rotation lever needed at GA).
"""

from __future__ import annotations

import hashlib
import hmac

import asyncpg

from polaris_api.services.secrets import (
    CryptoError,
    decrypt_platform_sentinel,
    encrypt_platform_sentinel,
)

# Fixed plaintext used to fingerprint the active POLARIS_KMS_KEY. The value
# itself is not a secret (it's checked into source); what matters is that
# encrypting it under the running key and SHA-256'ing the plaintext yields
# a hash that matches the stored value. Changing this constant after the
# first seed in any environment requires re-seeding everywhere.
SENTINEL_PLAINTEXT: bytes = b"polaris-v2-kms-sentinel-do-not-modify"


async def verify_sentinel(conn: asyncpg.Connection) -> bool:
    """Verify the KMS sentinel against the current process key.

    Returns:
        True  — sentinel row exists and the hash matches.
        False — no sentinel row (= unseeded environment). Caller decides
                whether that's fatal (staging/prod) or acceptable (local).

    Raises:
        CryptoError — sentinel row exists but verification failed. Means
                      the running `POLARIS_KMS_KEY` differs from the one
                      used to seed the sentinel. Caller must abort startup;
                      proceeding would corrupt every subsequent secret
                      write under the wrong key.
    """
    row = await conn.fetchrow(
        "SELECT kms_sentinel_ciphertext, kms_sentinel_plaintext_hash "
        "FROM platform_health WHERE id = 1"
    )
    if row is None:
        return False

    plaintext = decrypt_platform_sentinel(row["kms_sentinel_ciphertext"])
    actual_hash = hashlib.sha256(plaintext).digest()
    expected_hash = bytes(row["kms_sentinel_plaintext_hash"])

    # Constant-time compare guards against timing-based oracle attacks on
    # boot — although in practice the attacker would already have the DB
    # row at that point, the cost of using compare_digest is zero.
    if not hmac.compare_digest(actual_hash, expected_hash):
        raise CryptoError(
            "kms sentinel hash mismatch — POLARIS_KMS_KEY does not match "
            "the key used to seed platform_health"
        )
    return True


async def seed_sentinel(conn: asyncpg.Connection) -> None:
    """Write (or overwrite) the sentinel row for the current process key.

    Idempotent + safe to re-run: any existing row is replaced via UPSERT.
    Re-running with a different `POLARIS_KMS_KEY` is the intentional
    key-rotation path — combine with re-encryption of all `tenant_secrets`
    rows under the new key (a separate maintenance script, out of scope
    for PR-3).
    """
    ct = encrypt_platform_sentinel(SENTINEL_PLAINTEXT)
    h = hashlib.sha256(SENTINEL_PLAINTEXT).digest()
    await conn.execute(
        """
        INSERT INTO platform_health (id, kms_sentinel_ciphertext, kms_sentinel_plaintext_hash)
        VALUES (1, $1, $2)
        ON CONFLICT (id) DO UPDATE
        SET kms_sentinel_ciphertext     = EXCLUDED.kms_sentinel_ciphertext,
            kms_sentinel_plaintext_hash = EXCLUDED.kms_sentinel_plaintext_hash,
            last_verified_at            = now()
        """,
        ct,
        h,
    )
