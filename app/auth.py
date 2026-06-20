"""Supabase JWT verification for FastAPI (Phase 7.1).

Access tokens minted by Supabase Auth are ES256-signed with the project's
asymmetric key. We verify them against the project's public JWKS endpoint, so
no shared secret ever lives in the backend. The verified `sub` claim is the
user's UUID — every data query is scoped to it.
"""
import os

import jwt
from fastapi import Depends, HTTPException, Request
from jwt import PyJWKClient

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
# Supabase issues tokens with this issuer/audience.
_ISSUER = f"{SUPABASE_URL}/auth/v1" if SUPABASE_URL else ""
_AUDIENCE = "authenticated"
_JWKS_URL = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json" if SUPABASE_URL else ""

# PyJWKClient fetches and caches the signing keys (one network call per cold
# start, then in-memory). Created lazily so the app imports without SUPABASE_URL
# set (e.g. in unit tests, where get_current_user is overridden).
_jwk_client: PyJWKClient | None = None


def _client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        if not _JWKS_URL:
            raise HTTPException(status_code=503, detail="Auth not configured")
        _jwk_client = PyJWKClient(_JWKS_URL)
    return _jwk_client


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return header[7:].strip()


def get_current_user(request: Request) -> str:
    """FastAPI dependency → the authenticated user's UUID (the JWT `sub`).

    Raises 401 on a missing/invalid/expired token. Use as
    `user_id: str = Depends(get_current_user)` on every user-scoped route.
    """
    token = _bearer(request)
    try:
        signing_key = _client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256"],
            audience=_AUDIENCE,
            issuer=_ISSUER,
            options={"require": ["sub", "exp"]},
        )
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Token missing subject")
    return sub


# Convenience alias for route signatures.
CurrentUser = Depends(get_current_user)
