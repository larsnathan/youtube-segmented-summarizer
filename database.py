"""Very small SQLAlchemy + Alembic setup for tracking downloads."""

from pathlib import Path
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    MetaData,
    Table,
    select,
    text,
)
from sqlalchemy.orm import Session, registry

SCHEMA_VERSION = 2  #  ← bumped
DB_FILE = Path("/db/app.db")
ENGINE = create_engine(f"sqlite:///{DB_FILE}", future=True)
mapper_registry = registry()
metadata_obj: MetaData = mapper_registry.metadata

downloads = Table(
    "downloads",
    metadata_obj,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("uuid", String, unique=True, nullable=False),
    Column("title", String),
    Column("video", String),
    Column("caption", String),
    Column("summary", String),
    Column("status", String, default="downloaded"),
)

schema_info = Table(
    "schema_info",
    metadata_obj,
    Column("version", Integer, primary_key=True),
)


def _add_summary_column_if_needed() -> None:
    """Run `ALTER TABLE` at runtime if DB pre-dates v2."""
    with ENGINE.connect() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(downloads);"))]
        if "summary" not in cols:
            conn.execute(text("ALTER TABLE downloads ADD COLUMN summary TEXT;"))
            conn.commit()


def init_db() -> None:
    """Create or upgrade tables."""
    metadata_obj.create_all(ENGINE)

    with Session(ENGINE) as ses:
        ver_row = ses.execute(select(schema_info.c.version)).first()

        if ver_row is None:
            ses.execute(schema_info.insert().values(version=SCHEMA_VERSION))
            ses.commit()
        elif ver_row[0] < SCHEMA_VERSION:
            _add_summary_column_if_needed()
            ses.execute(schema_info.update().values(version=SCHEMA_VERSION))
            ses.commit()
        elif ver_row[0] > SCHEMA_VERSION:
            raise RuntimeError(
                f"Code expects schema {SCHEMA_VERSION}, but DB is newer ({ver_row[0]})."
            )
