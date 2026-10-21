"""
Database configuration.

Priority (highest → lowest)::

    1. ``DATABASE_URL`` set as a **real** environment variable
       (not from .env) — wins over everything, handy in Docker / CI / prod
    2. ``SQLITE_PATH=./dev.db``      → SQLite file (handy for local dev)
    3. ``USE_SQLITE=1``              → SQLite at ``./vehicle_test.db``
    4. ``DATABASE_URL`` from ``.env``, else the built-in PostgreSQL default

URL examples::

    PostgreSQL : postgresql+psycopg2://user:pass@host:5432/dbname
    SQLite     : sqlite:///./dev.db            (relative path)
                 sqlite:////abs/path/dev.db    (absolute path, 4 slashes)

Set these in ``.env`` (see .env.example) or as real environment variables.

Self-check::

    python -m app.config
"""
import os

from dotenv import load_dotenv

# snapshot *before* .env is merged, so we can tell "real env" from ".env"
_environ_before = dict(os.environ)

load_dotenv()

DEFAULT_POSTGRES_URL = (
    "postgresql+psycopg2://postgres:postgres@localhost:5432/vehicle_test"
)


def _build_url() -> str:
    # 1) real environment variable always wins (Docker/K8s/CI)
    explicit = _environ_before.get("DATABASE_URL", "").strip()
    if explicit:
        return explicit

    # 2) / 3) SQLite shortcuts
    sqlite_path = os.getenv("SQLITE_PATH", "").strip()
    use_sqlite = os.getenv("USE_SQLITE", "").strip().lower() in ("1", "true", "yes")
    if sqlite_path or use_sqlite:
        path = sqlite_path or "./vehicle_test.db"
        # sqlite:/// + ./dev.db   → sqlite:///./dev.db
        # sqlite:/// + /abs/path  → sqlite:////abs/path  (4 slashes)
        return path if path.startswith("sqlite:") else f"sqlite:///{path}"

    # 4) .env / default
    return os.getenv("DATABASE_URL", DEFAULT_POSTGRES_URL)


DATABASE_URL = _build_url()

# ── convenience flags used by database.py ──
IS_SQLITE = DATABASE_URL.startswith("sqlite")


def describe() -> str:
    """Human-readable summary, used by the CLI self-check below."""
    kind = "SQLite" if IS_SQLITE else "PostgreSQL"
    return f"{kind}  →  {DATABASE_URL}"


if __name__ == "__main__":
    print(f"DATABASE_URL = {DATABASE_URL}")
    print(f"IS_SQLITE    = {IS_SQLITE}")
    print(f"Using        : {describe()}")
