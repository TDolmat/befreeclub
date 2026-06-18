"""Port core/auth/password.ts. Hashe scrypt kompatybilne bajt w bajt z Node:

scrypt$<N>$<r>$<p>$<saltHex>$<hashHex>

Weryfikacja czyta parametry ze stringa (nie ze stalych) - dziala tez dummy hash
scrypt$65536$8$1$00$00 (dklen=1) uzywany anty-enumeracyjnie w loginie.
"""

import asyncio
import hashlib
import hmac
import secrets

SCRYPT_N = 2**16
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64
SALT_LEN = 16
SCRYPT_MAXMEM = 256 * 1024 * 1024

DUMMY_HASH = "scrypt$65536$8$1$00$00"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_LEN)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n = int(parts[1], 10)
        r = int(parts[2], 10)
        p = int(parts[3], 10)
    except ValueError:
        return False
    try:
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
    except ValueError:
        return False
    try:
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=SCRYPT_MAXMEM,
        )
    except Exception:
        return False
    if len(derived) != len(expected):
        return False
    return hmac.compare_digest(derived, expected)


async def hash_password_async(password: str) -> str:
    """scrypt jest CPU-bound (~150 ms) - w kodzie async uzywaj tej wersji."""
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, stored: str) -> bool:
    return await asyncio.to_thread(verify_password, password, stored)
