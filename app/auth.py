"""Supabase JWT verification for FastAPI (Phase 7.1).

Access tokens minted by Supabase Auth are ES256-signed with the project's
asymmetric key. We verify them against the project's public JWKS endpoint, so
no shared secret ever lives in the backend. The verified `sub` claim is the
user's UUID — every data query is scoped to it.
"""
import logging
import os

import httpx
import jwt
from fastapi import Depends, HTTPException, Request
from jwt import PyJWKClient

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
# Service-role key (server-only secret) — needed to delete a user's auth record
# via the Supabase admin API on account deletion. Never sent to clients; lives
# only in the backend env (Vercel). Absent in tests/local, so deletion of the
# auth record is best-effort and skipped when unset.
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
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


async def delete_auth_user(user_id: str) -> bool:
    """Delete the user's Supabase Auth record via the admin API.

    Used on account deletion so the account itself is gone, not just its data.
    Best-effort: returns False (and logs) when the service-role key isn't
    configured or the call fails, so the caller can still report that the data
    was wiped. A 404 from Supabase (user already gone) counts as success.
    """
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        logging.warning("SUPABASE_SERVICE_ROLE_KEY not set — skipping auth-user delete")
        return False
    url = f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}"
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    }
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            res = await client.delete(url, headers=headers)
        if res.status_code in (200, 204, 404):
            return True
        logging.error("auth-user delete failed: %s %s", res.status_code, res.text[:200])
        return False
    except Exception:
        logging.exception("auth-user delete request errored")
        return False


# Convenience alias for route signatures.
CurrentUser = Depends(get_current_user)
