import threading
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from lelia.config.settings import base_dir, ensure_dirs


class Base(DeclarativeBase):
    pass


_engines: dict[str, object] = {}
_engines_lock = threading.Lock()


def _session_factory(engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


def _engine_for(path: Path):
    """Engine cacheado por arquivo — evita vazamento de conexões e habilita WAL."""
    key = str(path)
    with _engines_lock:
        engine = _engines.get(key)
        if engine is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            engine = create_engine(
                f"sqlite:///{path}",
                connect_args={"check_same_thread": False, "timeout": 30},
            )

            @event.listens_for(engine, "connect")
            def _set_pragmas(dbapi_conn, _):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()

            _engines[key] = engine
        return engine


def get_global_engine():
    ensure_dirs()
    return _engine_for(base_dir() / "lelia.db")


def get_project_engine(slug: str):
    return _engine_for(base_dir() / "projects" / slug / "db.sqlite")


def init_db() -> None:
    from lelia.storage import models_global, models_project  # noqa: F401

    Base.metadata.create_all(get_global_engine())


def get_global_session() -> Session:
    init_db()
    return _session_factory(get_global_engine())()


_migrated: set[str] = set()


def _migrate_project(slug: str) -> None:
    """ALTERs incrementais para bancos de projeto já existentes."""
    if slug in _migrated:
        return
    from sqlalchemy import text
    eng = get_project_engine(slug)
    with eng.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(paper)"))}
        if cols and "pdf_hash" not in cols:
            conn.execute(text("ALTER TABLE paper ADD COLUMN pdf_hash VARCHAR(64) DEFAULT ''"))
        lp_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(llm_provider)"))}
        if lp_cols and "last_error" not in lp_cols:
            conn.execute(text("ALTER TABLE llm_provider ADD COLUMN last_error TEXT DEFAULT ''"))
        if lp_cols and "last_sync" not in lp_cols:
            conn.execute(text("ALTER TABLE llm_provider ADD COLUMN last_sync DATETIME"))
    _migrated.add(slug)


def get_project_session(slug: str) -> Session:
    from lelia.storage import models_project  # noqa: F401

    Base.metadata.create_all(get_project_engine(slug))
    _migrate_project(slug)
    return _session_factory(get_project_engine(slug))()
