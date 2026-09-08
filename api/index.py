"""Vercel serverless entry point.

Vercel's Python runtime looks for `app` in a file under /api. This module exists
only to expose the FastAPI application from that location; all logic lives in
apix/api/.

WHY THE POOL IS DISABLED HERE
-----------------------------
`apix/db.py` builds a process-global connection pool, which is right for a
long-lived uvicorn server and wrong for serverless, where every invocation is its
own short-lived process. A pool per invocation multiplies connections by
concurrency and exhausts Postgres `max_connections` under a modest spike.

So on Vercel we set APIX_DISABLE_POOL and rely on the database's own pooler:
Neon's `-pooler` host, or PgBouncer in transaction mode. The connection string in
DATABASE_URL must point at that pooled endpoint, not the direct one.
"""
import os

os.environ.setdefault("APIX_DISABLE_POOL", "true")

from apix.api.main import app  # noqa: E402,F401
