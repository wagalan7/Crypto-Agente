import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./content_agency.db")

# Railway/Heroku ship "postgres://"; SQLAlchemy 2.x requires "postgresql://".
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from models import db_models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _run_lightweight_migrations()


def _run_lightweight_migrations():
    """Add new columns to existing tables when upgrading schema.

    SQLAlchemy create_all only creates new tables — it never ALTERs.
    We use IF NOT EXISTS (Postgres 9.6+) so it's safe to run every startup.
    SQLite doesn't support IF NOT EXISTS on ADD COLUMN, so we check pragma.
    """
    from sqlalchemy import text, inspect

    is_sqlite = DATABASE_URL.startswith("sqlite")
    inspector = inspect(engine)

    # ContentPiece new columns (strategic reasoning)
    content_columns = {
        "objective_reasoning": "TEXT",
        "emotion_used": "VARCHAR(100)",
        "funnel_stage": "VARCHAR(50)",
        "format_reasoning": "TEXT",
        "linked_product_id": "INTEGER",
        "production_brief": "TEXT",
        "voice_score": "INTEGER",
        "voice_feedback": "TEXT",
    }

    if "content_pieces" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("content_pieces")}
        with engine.begin() as conn:
            for col, coltype in content_columns.items():
                if col in existing:
                    continue
                if is_sqlite:
                    conn.execute(text(f"ALTER TABLE content_pieces ADD COLUMN {col} {coltype}"))
                else:
                    conn.execute(text(f"ALTER TABLE content_pieces ADD COLUMN IF NOT EXISTS {col} {coltype}"))

    # Users new columns (Phase 13: plans + onboarding + Stripe)
    user_columns = {
        "plan_tier": "VARCHAR(20) DEFAULT 'free'",
        "plan_status": "VARCHAR(20) DEFAULT 'active'",
        "trial_ends_at": "TIMESTAMP",
        "stripe_customer_id": "VARCHAR(120)",
        "stripe_subscription_id": "VARCHAR(120)",
        "onboarding_completed": "BOOLEAN DEFAULT FALSE",
    }
    if "users" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("users")}
        with engine.begin() as conn:
            for col, coltype in user_columns.items():
                if col in existing:
                    continue
                if is_sqlite:
                    conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {coltype}"))
                else:
                    conn.execute(text(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {coltype}"))
