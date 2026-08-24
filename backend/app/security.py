import base64
import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime, timedelta

import jwt


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_secret(value: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(value.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${_encode(salt)}${_encode(digest)}"


def verify_secret(value: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            value.encode(), salt=_decode(salt), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(actual, _decode(expected))
    except (TypeError, ValueError):
        return False


def random_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def create_access_token(user_id: str, secret: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {"sub": user_id, "type": "access", "iat": now, "exp": now + timedelta(days=7)},
        secret,
        algorithm="HS256",
    )


def decode_access_token(token: str, secret: str) -> str | None:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        if payload.get("type") != "access" or not payload.get("sub"):
            return None
        return str(payload["sub"])
    except jwt.PyJWTError:
        return None
