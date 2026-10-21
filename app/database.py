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


# ── 连接自检 ────────────────────────────────────────────────────────────
# Windows 中文版上，libpq 返回的错误消息是 GBK 编码（"致命错误: 用户
# \"postgres\" Password 认证失败"），而 psycopg2 一律按 UTF-8 解码，
# 于是真正的报错被 UnicodeDecodeError 盖掉，只剩一句看不懂的
# "'utf-8' codec can't decode byte 0xd6 ..."。
# 这里把原始字节捞回来，用 GBK 重新解一次，还原真正的原因。

def _decode_libpq_error(exc: UnicodeDecodeError) -> str:
    raw = exc.object
    if isinstance(raw, str):            # 不是 bytes 就没得救
        return str(exc)
    for enc in ("gb18030", "gbk", "big5", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:                               # pragma: no cover
        text = str(exc)
    # libpq 会在消息末尾带换行，顺手去掉
    return text.strip() or str(exc)


def probe() -> tuple[bool, str]:
    """真实连一次库。返回 (是否连上, 失败原因)。

    SQLite 永远返回 True —— 文件连不上时 sqlalchemy 自己会报清楚。
    """
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, ""
    except UnicodeDecodeError as exc:
        return False, _decode_libpq_error(exc)
    except Exception as exc:            # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def describe_failure(reason: str) -> str:
    """把连接失败包装成一段能照着修的提示。"""
    lines = [
        "",
        "=" * 72,
        " 数据库连接失败 —— API 会全部返回 500",
        "=" * 72,
        f" 目标: {DATABASE_URL}",
        f" 原因: {reason}",
        "",
    ]
    if not IS_SQLITE:
        lines += [
            " 常见处理:",
            "   1. 密码/库名不对 → 在 .env 里写正确的 DATABASE_URL:",
            "      DATABASE_URL=postgresql+psycopg2://用户:密码@localhost:5432/库名",
            "   2. 库还没建      → createdb -U postgres vehicle_test",
            "   3. 想用本地 SQLite（dev.db 已有数据）→ .env 里写:",
            "      SQLITE_PATH=./dev.db",
            "",
        ]
    lines.append("=" * 72)
    return "\n".join(lines)


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
