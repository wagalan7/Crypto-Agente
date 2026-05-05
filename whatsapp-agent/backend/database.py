from __future__ import annotations
import os
import sqlite3
from datetime import datetime
from contextlib import contextmanager

import config as _cfg
DB_PATH = os.path.join(_cfg.DATA_DIR, "consultorio.db")


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tenants (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                slug        TEXT NOT NULL UNIQUE,
                name        TEXT NOT NULL,
                psychologist_name TEXT NOT NULL DEFAULT 'Psicóloga',
                working_hours_start INTEGER NOT NULL DEFAULT 7,
                working_hours_end   INTEGER NOT NULL DEFAULT 21,
                session_minutes     INTEGER NOT NULL DEFAULT 50,
                whatsapp_provider   TEXT NOT NULL DEFAULT 'mock',
                evolution_url       TEXT DEFAULT '',
                evolution_key       TEXT DEFAULT '',
                evolution_instance  TEXT DEFAULT '',
                twilio_sid          TEXT DEFAULT '',
                twilio_token        TEXT DEFAULT '',
                twilio_from         TEXT DEFAULT '',
                active      INTEGER NOT NULL DEFAULT 1,
                dashboard_token TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS appointments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id    INTEGER NOT NULL REFERENCES tenants(id),
                patient_name TEXT NOT NULL,
                phone        TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                confirmed    INTEGER DEFAULT 0,
                notes        TEXT DEFAULT '',
                created_at   TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                phone     TEXT NOT NULL,
                role      TEXT NOT NULL,
                content   TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_appointments_tenant  ON appointments(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_appointments_phone   ON appointments(phone);
            CREATE INDEX IF NOT EXISTS idx_appointments_date    ON appointments(scheduled_at);
            CREATE INDEX IF NOT EXISTS idx_conversations_tenant ON conversations(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_conversations_phone  ON conversations(phone);
        """)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Tenants ────────────────────────────────────────────────────────────────────

def create_tenant(slug: str, name: str, psychologist_name: str = "Psicóloga",
                  working_hours_start: int = 7, working_hours_end: int = 21,
                  session_minutes: int = 50) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO tenants
               (slug, name, psychologist_name, working_hours_start, working_hours_end, session_minutes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (slug, name, psychologist_name, working_hours_start, working_hours_end, session_minutes),
        )
        return cur.lastrowid


def get_tenant(slug: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE slug = ? AND active = 1", (slug,)).fetchone()
    return dict(row) if row else None


def get_tenant_by_id(tenant_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    return dict(row) if row else None


def get_tenant_by_token(token: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE dashboard_token = ? AND active = 1", (token,)
        ).fetchone()
    return dict(row) if row else None


def list_tenants() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM tenants WHERE active = 1 ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def update_tenant(slug: str, **fields) -> bool:
    allowed = {
        "name", "psychologist_name", "working_hours_start", "working_hours_end",
        "session_minutes", "whatsapp_provider", "evolution_url", "evolution_key",
        "evolution_instance", "twilio_sid", "twilio_token", "twilio_from", "active",
        "dashboard_token",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [slug]
    with get_conn() as conn:
        cur = conn.execute(f"UPDATE tenants SET {set_clause} WHERE slug = ?", values)
        return cur.rowcount > 0


# ── Conversations ──────────────────────────────────────────────────────────────

def get_conversation_history(tenant_id: int, phone: str, limit: int = 10) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT role, content FROM conversations
               WHERE tenant_id = ? AND phone = ?
               ORDER BY created_at DESC LIMIT ?""",
            (tenant_id, phone, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def save_message(tenant_id: int, phone: str, role: str, content: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO conversations (tenant_id, phone, role, content) VALUES (?, ?, ?, ?)",
            (tenant_id, phone, role, content),
        )


def clear_conversation(tenant_id: int, phone: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM conversations WHERE tenant_id = ? AND phone = ?", (tenant_id, phone))


# ── Appointments ───────────────────────────────────────────────────────────────

def get_appointments_by_phone(tenant_id: int, phone: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM appointments
               WHERE tenant_id = ? AND phone = ? AND scheduled_at >= datetime('now')
               ORDER BY scheduled_at""",
            (tenant_id, phone),
        ).fetchall()
    return [dict(r) for r in rows]


def get_appointments_in_range(tenant_id: int, start: str, end: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM appointments
               WHERE tenant_id = ? AND scheduled_at BETWEEN ? AND ?
               ORDER BY scheduled_at""",
            (tenant_id, start, end),
        ).fetchall()
    return [dict(r) for r in rows]


def create_appointment(tenant_id: int, patient_name: str, phone: str,
                       scheduled_at: datetime, notes: str = "") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO appointments (tenant_id, patient_name, phone, scheduled_at, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (tenant_id, patient_name, phone, scheduled_at.isoformat(), notes),
        )
        return cur.lastrowid


def update_appointment(tenant_id: int, appointment_id: int, scheduled_at: datetime) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE appointments SET scheduled_at = ? WHERE id = ? AND tenant_id = ?",
            (scheduled_at.isoformat(), appointment_id, tenant_id),
        )
        return cur.rowcount > 0


def confirm_appointment(tenant_id: int, appointment_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE appointments SET confirmed = 1 WHERE id = ? AND tenant_id = ?",
            (appointment_id, tenant_id),
        )
        return cur.rowcount > 0


def is_slot_taken(tenant_id: int, dt: datetime) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM appointments WHERE tenant_id = ? AND scheduled_at = ?",
            (tenant_id, dt.isoformat()),
        ).fetchone()
    return row is not None
