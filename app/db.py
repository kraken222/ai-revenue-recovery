from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401  (register models on Base before create_all)

    Base.metadata.create_all(bind=engine)


def make_session_factory(database_url: str):
    """An isolated engine + session factory, independent of the module-level default.
    Used by the ablation harness so each configuration runs against its own clean
    database rather than contaminating a shared one.

    Returns (factory, engine) so the caller can dispose the engine — on Windows an
    undisposed SQLite engine keeps the file handle open and the temp file cannot be
    removed.

    In-memory URLs need StaticPool: the default pool hands out a new connection per
    checkout, and each new connection to ":memory:" is a brand-new empty database, so
    the schema would vanish between sessions.
    """
    from app import models  # noqa: F401

    args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    kwargs = {"connect_args": args}
    if ":memory:" in database_url:
        kwargs["poolclass"] = StaticPool

    isolated_engine = create_engine(database_url, **kwargs)
    Base.metadata.create_all(bind=isolated_engine)
    factory = sessionmaker(bind=isolated_engine, autoflush=False, autocommit=False)
    return factory, isolated_engine


def schema_drift(target_engine=None) -> list[str]:
    """Columns the models declare that the database does not have.

    `create_all` adds missing TABLES but never missing COLUMNS, so a database created
    before a field was added keeps working right up until something selects that field —
    and then every request fails with an OperationalError that reads like a server
    fault. This turns that into one legible line at startup.

    Not a migration system, and not pretending to be one: it detects drift and says
    what to do about it. A real deployment needs Alembic.
    """
    from app import models  # noqa: F401  (register models on Base)

    engine = target_engine or globals()["engine"]
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    problems: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing_tables:
            # A missing table is not drift — create_all will add it on next init.
            continue
        actual = {c["name"] for c in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name not in actual:
                problems.append(f"{table_name}.{column.name} is missing from the database")
    return problems
