# django-rustyhip

Django database backend that speaks to a [rustyhip](https://github.com/monkut/rustyhip)
Lambda over HTTP. The Lambda backs SQLite with Turbolite's S3-backed VFS, so your
Django app gets a SQL database that lives in an S3 bucket instead of RDS/Aurora —
no VPC, no idle floor cost.

**Status:** experimental. Proves Django `migrate` + basic ORM operations end-to-end
through a rustyhip endpoint. Not production-ready — see "Known limitations" below.

## Requirements

- Python 3.11+
- Django 4.2 / 5.x / 6.x
- A reachable rustyhip endpoint (local `cargo lambda watch` or a deployed Lambda)

## Install

```bash
pip install django-rustyhip
# or, for local development against a checkout:
uv add --editable /path/to/django-rustyhip
```

## Configure

```python
# settings.py
DATABASES = {
    "default": {
        "ENGINE": "rustyhip",
        "NAME": "rustyhip",                    # logical DB name, informational only
        "HOST": "http://localhost:9000",        # rustyhip Lambda URL
        "PASSWORD": os.environ.get("RUSTYHIP_AUTH_TOKEN", ""),  # bearer token (optional)
    }
}
```

`OPTIONS["timeout"]` (seconds, default `30`) can be added to tune the HTTP request timeout.

**Backward-compatible:** `OPTIONS["endpoint"]` still works if `HOST` is not set.

**Using environment variables (recommended for deployed apps):**

```python
# settings.py
import os

DATABASES = {
    "default": {
        "ENGINE": "rustyhip",
        "NAME": "rustyhip",
        "HOST": os.environ["RUSTYHIP_ENDPOINT"],        # e.g. https://abc123.execute-api.us-west-2.amazonaws.com/dev
        "PASSWORD": os.environ.get("RUSTYHIP_AUTH_TOKEN", ""),  # bearer token
    }
}
```

Then:

```bash
python manage.py migrate
```

## How it works

rustyhip exposes a minimal HTTP surface:

```
POST /sql  { "sql": "...", "params": [...] }
    ⇓
{ "columns": [...], "rows": [...], "rowcount": N, "lastrowid": M, "readonly": bool }
```

This backend subclasses `django.db.backends.sqlite3` (so Django's SQLite
dialect, introspection, and schema editor still apply) but replaces the
connection layer. Instead of `sqlite3.connect()`, each cursor `execute`
call POSTs to `/sql` and materializes the JSON response back into
DB-API-shaped rows.

Transactions (`BEGIN` / `COMMIT` / `ROLLBACK` / `SAVEPOINT` / `RELEASE`)
are swallowed on the client side — each `/sql` call is a single statement
that auto-commits server-side.

## Known limitations

- **No real transactions.** Failed migrations leave partial schema — you'll
  need to re-seed the S3 prefix and retry.
- **No blobs.** `BYTES` columns round-trip as null. Most Django auth / admin /
  sessions migrations don't need blobs.
- **No user-defined SQL functions.** Django registers a few (`django_date_extract`
  etc.) via `sqlite3.Connection.create_function` — we no-op them. Queries that
  use those functions will fail server-side with "no such function."
- **Writes are serialized by rustyhip's Lambda.** Single-writer, by design
  (`ReservedConcurrentExecutions: 1` in the deploy template).

## Development

```bash
uv sync
uv run pytest
uv run ruff check src/ tests/
uv run pyright src/
```

## License

`LicenseRef-KICONIAWORKS-Customers`. See `LICENSE`.
