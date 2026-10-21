"""Database engine and session management."""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import DATABASE_URL, IS_SQLITE

# Curve rows carry 10万点级别的 JSON。orjson 比 stdlib json 快 2~4 倍，
# 装了就自动用上（pip install orjson），没装也完全能跑。
try:
    import orjson as _orjson

    def _json_serializer(obj):
        return _orjson.dumps(obj).decode("utf-8")

    def _json_deserializer(s):
        return _orjson.loads(s)

except Exception:                                     # pragma: no cover
    _json_serializer = None
    _json_deserializer = None

_engine_kwargs = {}
if _json_serializer is not None:
    _engine_kwargs["json_serializer"] = _json_serializer
    _engine_kwargs["json_deserializer"] = _json_deserializer

if IS_SQLITE:
    # SQLite: no connection-pool args (unsupported), and FastAPI's
    # threadpool needs check_same_thread=False.
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        echo=False,
        **_engine_kwargs,
    )

    # SQLite ignores FOREIGN KEY clauses unless this pragma is on.
    @event.listens_for(engine, "connect")
    def _sqlite_enable_fk(dbapi_conn, _record):  # noqa: ANN001, ANN202
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
else:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,   # auto-reconnect stale connections
        pool_size=10,
        max_overflow=20,
        echo=False,           # set True for SQL logging
        **_engine_kwargs,
    )

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
    pass


def get_db():
    """FastAPI dependency – yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
