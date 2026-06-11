# Vercel serverless entrypoint. The @vercel/python builder requires a
# top-level "app" assignment (AST-checked) — keep the final line as-is.
import traceback

_tb = None
try:
    from app.main import app as _real_app
except Exception:
    _real_app = None
    _tb = traceback.format_exc()


async def _import_error_app(scope, receive, send):
    """Fallback that reports why the real app failed to import."""
    if scope["type"] != "http":
        return
    await send({
        "type": "http.response.start",
        "status": 500,
        "headers": [(b"content-type", b"text/plain; charset=utf-8")],
    })
    await send({"type": "http.response.body", "body": (_tb or "?").encode()})


app = _real_app or _import_error_app
