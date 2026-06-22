"""
database.py
------------
SQLAlchemy setup for the job queue. Replaces the old in-memory JOBS dict
with a real Postgres table (hosted on Neon) so job history survives
server restarts/redeploys.

Reads the connection string from the DATABASE_URL environment variable.
Locally, put it in a .env file (see .env.example). On Render, set it as
an environment variable in the service's dashboard.
"""

import os
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, String, Float, Boolean, DateTime, JSON, Text
from sqlalchemy.orm import sessionmaker, declarative_base

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Locally: add it to a .env file. "
        "On Render: set it as an environment variable in the service settings. "
        "It should look like: postgresql://user:password@host/dbname?sslmode=require"
    )

# Neon (and most managed Postgres hosts) give you a URL starting with
# "postgresql://" — SQLAlchemy's psycopg2 driver wants the same prefix,
# so no rewriting needed. Some hosts hand out "postgres://" (note: no
# "ql") which SQLAlchemy rejects, so we normalize just in case.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class JobRecord(Base):
    """
    Persisted version of the old in-memory Job class. Mirrors its fields
    closely so the rest of the app (to_dict, progress hooks, etc.) needs
    minimal changes.
    """
    __tablename__ = "jobs"

    id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False, index=True)  # scopes jobs to a browser identity (see auth.py)
    url = Column(String, nullable=False)
    mode = Column(String, nullable=False)
    options = Column(JSON, default=dict)

    status = Column(String, nullable=False, default="queued")
    progress = Column(Float, default=0.0)
    speed = Column(String, nullable=True)
    eta = Column(String, nullable=True)
    filename = Column(String, nullable=True)
    size_downloaded = Column(String, nullable=True)
    size_total = Column(String, nullable=True)

    # R2 storage info, filled in once the finished file is uploaded
    storage_key = Column(String, nullable=True)   # path/key inside the R2 bucket
    download_ready = Column(Boolean, default=False)

    log = Column(JSON, default=list)
    error = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    cancel_requested = Column(Boolean, default=False)


def init_db():
    """Create tables if they don't exist yet. Safe to call on every startup."""
    Base.metadata.create_all(bind=engine)
    _migrate_add_user_id()


def _migrate_add_user_id():
    """
    create_all() only creates brand-new tables — it won't add a column to
    a 'jobs' table that already exists from before user_id was introduced.
    This adds it by hand if missing, so existing deployments don't crash
    on startup or lose their job history. Old rows get a placeholder
    user_id ('legacy') since we have no way to know who they belonged to;
    they just won't show up under anyone's new per-browser identity.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "jobs" not in inspector.get_table_names():
        return  # brand-new DB, create_all already made it correctly

    columns = [col["name"] for col in inspector.get_columns("jobs")]
    if "user_id" in columns:
        return  # already migrated

    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE jobs ADD COLUMN user_id VARCHAR"))
        conn.execute(text("UPDATE jobs SET user_id = 'legacy' WHERE user_id IS NULL"))
        conn.execute(text("ALTER TABLE jobs ALTER COLUMN user_id SET NOT NULL"))


def get_db():
    """FastAPI dependency — yields a session, always closes it after."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()