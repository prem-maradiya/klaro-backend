"""Error tracking + rate limiting for the API (Phase 7.5 ops hardening).

Both are opt-in and degrade to no-ops so local dev and the test suite run with
zero extra config:

- **Sentry** turns on only when `SENTRY_DSN` is set; otherwise `init_sentry()`
  is a harmless no-op. PII is never sent (this is a finance app).
- **Rate limiting** (SlowAPI) is on by default but keyed per authenticated user
  (falling back to client IP) and sized generously; flip `RATELIMIT_ENABLED=false`
  to disable. In-memory storage by default — fine for a single warm instance;
  point `RATELIMIT_STORAGE_URI` at Redis (e.g. Upstash) for shared limits across
  serverless instances.
"""
import logging
import os

import jwt
from slowapi import Limiter
from slowapi.util import get_remote_address

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Sentry — error tracking on the FastAPI surface.
# --------------------------------------------------------------------------- #
def init_sentry() -> bool:
    """Initialize Sentry if `SENTRY_DSN` is configured. Returns True when it was
    enabled. Never raises — a monitoring failure must not take down the API."""
    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        return False
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=dsn,
            # Tag events by environment so prod/staging are separable.
            environment=os.getenv("SENTRY_ENVIRONMENT")
            or os.getenv("VERCEL_ENV", "production"),
            release=os.getenv("VERCEL_GIT_COMMIT_SHA"),
            # Performance tracing is sampled (0 = errors only by default) to stay
            # inside the free event budget; bump via env if needed.
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0")),
            # Financial data: never attach request bodies, headers or user IPs.
            send_default_pii=False,
        )
        # sentry-sdk auto-instruments FastAPI/Starlette once init() has run.
        log.info("Sentry enabled")
        return True
    except Exception:
        log.exception("Sentry init failed — continuing without it")
        return False


# --------------------------------------------------------------------------- #
# Rate limiting — abuse / runaway-cost protection.
# --------------------------------------------------------------------------- #
def _rate_key(request) -> str:
    """Bucket requests by the authenticated user, falling back to client IP.

    The JWT `sub` is read WITHOUT verifying the signature — that's safe here
    because this value only chooses a counter bucket, it grants no access (the
    real verification still happens in `get_current_user`). Per-user keying means
    one abusive account can't exhaust the limit for everyone behind a shared NAT.
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
            sub = claims.get("sub")
            if sub:
                return f"user:{sub}"
        except Exception:
            pass
    # Behind Vercel/any proxy the real client is the first X-Forwarded-For hop;
    # request.client.host would otherwise be the proxy and lump everyone together.
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return f"ip:{xff.split(',')[0].strip()}"
    return f"ip:{get_remote_address(request)}"


# Default cap applied to every route via SlowAPIMiddleware; per-route limits can
# stack on top of it. Tunable without a code change.
#
# Sized to Klaro's real bursts, not generic API defaults: a full inbox
# import/re-import POSTs every parsed SMS sequentially (~125-200 rows) and a cold
# app open fires several GETs plus pull-reconcile — a worst-case legitimate
# minute is ~220+ requests. 600/min clears that comfortably while still capping a
# runaway/malicious client.
RATE_DEFAULT = os.getenv("RATELIMIT_DEFAULT", "600/minute")

limiter = Limiter(
    key_func=_rate_key,
    default_limits=[RATE_DEFAULT],
    storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    enabled=os.getenv("RATELIMIT_ENABLED", "true").lower() not in ("false", "0", "no"),
    # Keep success-path X-RateLimit-* headers OFF: with them on, SlowAPI's
    # per-route @limit decorator requires every decorated handler to expose a
    # `response` parameter or it raises at runtime — an easy footgun. The 429
    # response still carries Retry-After (added by the exceeded handler), which
    # is the header clients actually need.
    headers_enabled=False,
)
