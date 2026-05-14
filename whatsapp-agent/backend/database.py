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

            CREATE TABLE IF NOT EXISTS agent_paused (
                tenant_id INTEGER NOT NULL,
                phone     TEXT NOT NULL,
                paused_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (tenant_id, phone)
            );

            CREATE TABLE IF NOT EXISTS patients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL,
                phone TEXT NOT NULL,
                name TEXT DEFAULT '',
                session_price REAL DEFAULT 0,
                email TEXT DEFAULT '',
                UNIQUE(tenant_id, phone)
            );

            CREATE TABLE IF NOT EXISTS billing_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL,
                phone TEXT NOT NULL,
                patient_name TEXT NOT NULL,
                month TEXT NOT NULL,
                sessions_count INTEGER DEFAULT 0,
                total_amount REAL DEFAULT 0,
                sent_at TEXT DEFAULT (datetime('now')),
                channel TEXT DEFAULT 'whatsapp'
            );
        """)
        # Migrações incrementais — seguro rodar múltiplas vezes
        migrations = [
            "ALTER TABLE tenants ADD COLUMN dashboard_token TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN setup_token TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN google_refresh_token TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN google_calendar_id TEXT DEFAULT 'primary'",
            "ALTER TABLE appointments ADD COLUMN google_event_id TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN email TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN status TEXT DEFAULT 'active'",
            "ALTER TABLE tenants ADD COLUMN stripe_customer_id TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN stripe_subscription_id TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN mp_subscription_id TEXT DEFAULT ''",
            "ALTER TABLE appointments ADD COLUMN confirmation_sent INTEGER DEFAULT 0",
            "ALTER TABLE appointments ADD COLUMN followup_sent INTEGER DEFAULT 0",
            "ALTER TABLE tenants ADD COLUMN pix_key TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN pix_name TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN working_days TEXT DEFAULT '0,1,2,3,4'",
            "ALTER TABLE tenants ADD COLUMN blocked_hours TEXT DEFAULT '12,13,14'",
            "ALTER TABLE tenants ADD COLUMN confirmation_hour INTEGER DEFAULT 17",
            "ALTER TABLE tenants ADD COLUMN psychologist_phone TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN plan TEXT DEFAULT 'mensal'",
            "ALTER TABLE tenants ADD COLUMN free_until TEXT DEFAULT NULL",
            "ALTER TABLE appointments ADD COLUMN cancelled INTEGER DEFAULT 0",
            "ALTER TABLE tenants ADD COLUMN plan_expires_at TEXT DEFAULT NULL",
        ]
        for sql in migrations:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # coluna já existe


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


def get_tenant_by_setup_token(token: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE setup_token = ? AND active = 1", (token,)
        ).fetchone()
    return dict(row) if row else None


def is_tenant_exempt(tenant: dict) -> bool:
    """Retorna True se o tenant tem acesso gratuito ativo (free_until no futuro)."""
    free_until = tenant.get("free_until")
    if not free_until:
        return False
    from datetime import date
    try:
        return date.fromisoformat(free_until[:10]) >= date.today()
    except ValueError:
        return False


def list_tenants() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM tenants WHERE active = 1 ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def update_tenant(slug: str, **fields) -> bool:
    allowed = {
        "name", "psychologist_name", "working_hours_start", "working_hours_end",
        "session_minutes", "whatsapp_provider", "evolution_url", "evolution_key",
        "evolution_instance", "twilio_sid", "twilio_token", "twilio_from", "active",
        "dashboard_token", "setup_token", "google_refresh_token", "google_calendar_id",
        "email", "status", "stripe_customer_id", "stripe_subscription_id", "mp_subscription_id",
        "pix_key", "pix_name",
        "working_days", "blocked_hours", "confirmation_hour", "psychologist_phone", "plan",
        "free_until", "plan_expires_at",
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


def is_agent_paused(tenant_id: int, phone: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM agent_paused WHERE tenant_id = ? AND phone = ?",
            (tenant_id, phone)
        ).fetchone()
    return row is not None


def pause_agent(tenant_id: int, phone: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO agent_paused (tenant_id, phone) VALUES (?, ?)",
            (tenant_id, phone)
        )


def resume_agent(tenant_id: int, phone: str):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM agent_paused WHERE tenant_id = ? AND phone = ?",
            (tenant_id, phone)
        )


def list_paused_phones(tenant_id: int) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT phone FROM agent_paused WHERE tenant_id = ?", (tenant_id,)
        ).fetchall()
    return [r["phone"] for r in rows]


def clear_conversation(tenant_id: int, phone: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM conversations WHERE tenant_id = ? AND phone = ?", (tenant_id, phone))


# ── Appointments ───────────────────────────────────────────────────────────────

def get_appointments_by_phone(tenant_id: int, phone: str, now_iso: str | None = None) -> list[dict]:
    # Appointments are stored in Brasília time (naive). Use now_iso passed from caller
    # to avoid comparing against SQLite's datetime('now') which is UTC.
    if now_iso is None:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now_iso = datetime.now(ZoneInfo("America/Sao_Paulo")).replace(tzinfo=None).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM appointments
               WHERE tenant_id = ? AND phone = ? AND scheduled_at >= ?
               ORDER BY scheduled_at""",
            (tenant_id, phone, now_iso),
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


def get_appointment_by_id(tenant_id: int, appointment_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM appointments WHERE id = ? AND tenant_id = ?",
            (appointment_id, tenant_id),
        ).fetchone()
    return dict(row) if row else None


def set_appointment_google_event_id(appointment_id: int, event_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE appointments SET google_event_id = ? WHERE id = ?",
            (event_id, appointment_id),
        )


def mark_confirmation_sent(appointment_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE appointments SET confirmation_sent = 1 WHERE id = ?",
            (appointment_id,),
        )


def mark_followup_sent(appointment_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE appointments SET followup_sent = 1 WHERE id = ?",
            (appointment_id,),
        )


def get_appointments_for_confirmation(tenant_id: int) -> list[dict]:
    """Retorna consultas nas próximas 25h que ainda não receberam confirmação.
    Cobre tanto o fluxo normal (24h antes) quanto agendamentos de última hora
    (feitos com menos de 23h de antecedência — antes ignorados pela janela fixa).
    Usa horário de Brasília passado pelo Python (evita bug de UTC vs localtime no SQLite)."""
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("America/Sao_Paulo")
    now_br = _dt.now(_TZ).replace(tzinfo=None)  # naive, mesmo formato do scheduled_at
    # Janela: de 1h até 36h no futuro.
    # 36h garante que consultas de "amanhã" são sempre capturadas independentemente
    # do horário atual (ex: consulta às 10:50 com 25h15min de distância não fica fora).
    # O flag confirmation_sent=0 evita envios duplicados.
    window_start = (now_br + _td(hours=1)).isoformat(timespec="seconds")
    window_end   = (now_br + _td(hours=36)).isoformat(timespec="seconds")
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM appointments
               WHERE tenant_id = ?
                 AND confirmation_sent = 0
                 AND cancelled = 0
                 AND scheduled_at > ?
                 AND scheduled_at <= ?
               ORDER BY scheduled_at""",
            (tenant_id, window_start, window_end),
        ).fetchall()
    return [dict(r) for r in rows]


def get_appointments_for_tomorrow(tenant_id: int) -> list[dict]:
    """Alias de compatibilidade — retorna consultas nas próximas 24-25h sem confirmação."""
    return get_appointments_for_confirmation(tenant_id)


def get_appointments_today_unconfirmed(tenant_id: int) -> list[dict]:
    """Retorna consultas de hoje que ainda não foram confirmadas e o followup não foi enviado.
    Usa horário de Brasília passado pelo Python."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("America/Sao_Paulo")
    now_br = _dt.now(_TZ).replace(tzinfo=None)
    today_str = now_br.date().isoformat()          # 'YYYY-MM-DD'
    now_str   = now_br.isoformat(timespec="seconds")
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM appointments
               WHERE tenant_id = ?
                 AND date(scheduled_at) = ?
                 AND confirmed = 0
                 AND followup_sent = 0
                 AND cancelled = 0
                 AND scheduled_at > ?
               ORDER BY scheduled_at""",
            (tenant_id, today_str, now_str),
        ).fetchall()
    return [dict(r) for r in rows]


def is_slot_taken(tenant_id: int, dt: datetime) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM appointments WHERE tenant_id = ? AND scheduled_at = ?",
            (tenant_id, dt.isoformat()),
        ).fetchone()
    return row is not None


# ── Patients ───────────────────────────────────────────────────────────────────

def upsert_patient(tenant_id: int, phone: str, name: str = "", session_price: float = 0.0, email: str = "") -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO patients (tenant_id, phone, name, session_price, email)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, phone) DO UPDATE SET
                name = COALESCE(NULLIF(excluded.name,''), patients.name),
                session_price = excluded.session_price,
                email = COALESCE(NULLIF(excluded.email,''), patients.email)
        """, (tenant_id, phone, name, session_price, email))


def get_patient(tenant_id: int, phone: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM patients WHERE tenant_id = ? AND phone = ?",
            (tenant_id, phone)
        ).fetchone()
    return dict(row) if row else None


def get_patients_with_price(tenant_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM patients WHERE tenant_id = ? AND session_price > 0 ORDER BY name",
            (tenant_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_valid_sessions_for_month(tenant_id: int, phone: str, month_start: str, month_end: str, now_str: str) -> list[dict]:
    """Sessions that are confirmed AND already occurred (date passed) within the month."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM appointments
            WHERE tenant_id = ? AND phone = ?
              AND confirmed = 1
              AND scheduled_at >= ? AND scheduled_at < ?
              AND scheduled_at <= ?
            ORDER BY scheduled_at
        """, (tenant_id, phone, month_start, month_end, now_str)).fetchall()
    return [dict(r) for r in rows]


# ── Billing logs ───────────────────────────────────────────────────────────────

def billing_already_sent(tenant_id: int, phone: str, month: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM billing_logs WHERE tenant_id = ? AND phone = ? AND month = ?",
            (tenant_id, phone, month)
        ).fetchone()
    return row is not None


def save_billing_log(tenant_id: int, phone: str, patient_name: str, month: str,
                     sessions_count: int, total_amount: float, channel: str = "whatsapp"):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO billing_logs (tenant_id, phone, patient_name, month, sessions_count, total_amount, channel)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (tenant_id, phone, patient_name, month, sessions_count, total_amount, channel))


def get_billing_logs(tenant_id: int, limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM billing_logs WHERE tenant_id = ?
            ORDER BY sent_at DESC LIMIT ?
        """, (tenant_id, limit)).fetchall()
    return [dict(r) for r in rows]
