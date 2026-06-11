# Vercel serverless entrypoint — the platform detects the ASGI `app` and
# routes every request to it (see vercel.json rewrites).
from app.main import app  # noqa: F401
