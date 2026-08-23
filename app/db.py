from sqlalchemy import create_engine
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
