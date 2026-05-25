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
            "ALTER TABLE tenants ADD COLUMN caldav_url TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN caldav_username TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN caldav_password TEXT DEFAULT ''",
            # Dados de faturamento (obrigatórios no onboarding)
            "ALTER TABLE tenants ADD COLUMN full_name TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN cpf_cnpj TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN billing_zip TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN billing_address TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN billing_number TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN billing_complement TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN billing_neighborhood TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN billing_city TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN billing_state TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN phone TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN webhook_token TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN accepted_terms_at TEXT DEFAULT NULL",
            "ALTER TABLE tenants ADD COLUMN accepted_terms_version TEXT DEFAULT ''",
            "ALTER TABLE admin_users ADD COLUMN totp_secret TEXT DEFAULT ''",
            "ALTER TABLE admin_users ADD COLUMN totp_enabled INTEGER DEFAULT 0",
            # Comparecimento: pending | attended | missed_no_notice | missed_with_notice
            "ALTER TABLE appointments ADD COLUMN attendance TEXT DEFAULT 'pending'",
            # Bloqueios de datas específicas (feriados/férias) — CSV YYYY-MM-DD
            "ALTER TABLE tenants ADD COLUMN blocked_dates TEXT DEFAULT ''",
            # Templates de mensagem personalizáveis
            "ALTER TABLE tenants ADD COLUMN confirmation_msg_template TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN followup_msg_template TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN billing_msg_template TEXT DEFAULT ''",
        ]
        for sql in migrations:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # coluna já existe

        # ── Tabelas auxiliares (admin, tracking, CMS) ─────────────────────────
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS admin_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                email TEXT DEFAULT '',
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS admin_sessions (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS landing_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                viewed_at TEXT DEFAULT (datetime('now')),
                ip TEXT DEFAULT '',
                user_agent TEXT DEFAULT '',
                referrer TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS site_content (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_landing_views_date ON landing_views(viewed_at);
            CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires ON admin_sessions(expires_at);

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT (datetime('now')),
                actor TEXT DEFAULT '',
                action TEXT NOT NULL,
                target TEXT DEFAULT '',
                ip TEXT DEFAULT '',
                details TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
            CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor);

            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                ip TEXT DEFAULT '',
                success INTEGER NOT NULL DEFAULT 0,
                attempted_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_login_attempts_user ON login_attempts(username, attempted_at);
            CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip, attempted_at);
        """)

        # Seed do usuário admin padrão (alanmalta) se ainda não existir
        _seed_default_admin(conn)


def _seed_default_admin(conn):
    """Cria o usuário admin inicial se nenhum existir.
    Lê credenciais EXCLUSIVAMENTE de variáveis de ambiente. Se ADMIN_PASSWORD
    não estiver configurada, gera uma senha aleatória e LOGA UMA VEZ — a senha
    precisa ser anotada e usada no primeiro login. NUNCA usa senha hardcoded."""
    import os, hashlib, secrets as _secrets, logging as _logging
    _log = _logging.getLogger(__name__)
    row = conn.execute("SELECT COUNT(*) AS n FROM admin_users").fetchone()
    if row["n"] > 0:
        return
    username = os.getenv("ADMIN_USERNAME", "alanmalta")
    email    = os.getenv("ADMIN_EMAIL", "wagalan@gmail.com")
    password = os.getenv("ADMIN_PASSWORD", "")
    if not password:
        password = _secrets.token_urlsafe(16)
        _log.warning("=" * 70)
        _log.warning(f"⚠️  ADMIN_PASSWORD não configurada. Senha temporária gerada:")
        _log.warning(f"    usuário: {username}")
        _log.warning(f"    senha:   {password}")
        _log.warning(f"    Defina ADMIN_PASSWORD no Railway e reinicie OU troque a senha pelo painel.")
        _log.warning("=" * 70)
    salt = _secrets.token_hex(16)
    pw_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    conn.execute(
        "INSERT INTO admin_users (username, email, password_hash, salt) VALUES (?, ?, ?, ?)",
        (username, email, pw_hash, salt),
    )


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
    import secrets as _secrets
    webhook_token = _secrets.token_urlsafe(24)
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO tenants
               (slug, name, psychologist_name, working_hours_start, working_hours_end, session_minutes, webhook_token)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (slug, name, psychologist_name, working_hours_start, working_hours_end, session_minutes, webhook_token),
        )
        return cur.lastrowid


def ensure_webhook_token(tenant_id: int) -> str:
    """Backfill: garante que o tenant tem webhook_token; cria se vazio."""
    import secrets as _secrets
    with get_conn() as conn:
        row = conn.execute("SELECT webhook_token FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        if row and row["webhook_token"]:
            return row["webhook_token"]
        token = _secrets.token_urlsafe(24)
        conn.execute("UPDATE tenants SET webhook_token = ? WHERE id = ?", (token, tenant_id))
        return token


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
        "working_days", "blocked_hours", "blocked_dates", "confirmation_hour", "psychologist_phone", "plan",
        "confirmation_msg_template", "followup_msg_template", "billing_msg_template",
        "free_until", "plan_expires_at",
        "caldav_url", "caldav_username", "caldav_password",
        "full_name", "cpf_cnpj", "phone",
        "billing_zip", "billing_address", "billing_number", "billing_complement",
        "billing_neighborhood", "billing_city", "billing_state",
        "accepted_terms_at", "accepted_terms_version",
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


def _norm_digits(phone: str) -> str:
    """Remove tudo que não for dígito — garante consistência na tabela agent_paused."""
    return "".join(c for c in (phone or "") if c.isdigit())


def is_agent_paused(tenant_id: int, phone: str) -> bool:
    phone = _norm_digits(phone)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM agent_paused WHERE tenant_id = ? AND phone = ?",
            (tenant_id, phone)
        ).fetchone()
    return row is not None


def pause_agent(tenant_id: int, phone: str):
    phone = _norm_digits(phone)
    if not phone:
        return
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO agent_paused (tenant_id, phone) VALUES (?, ?)",
            (tenant_id, phone)
        )


def resume_agent(tenant_id: int, phone: str):
    phone = _norm_digits(phone)
    if not phone:
        return
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


def pause_all_agents(tenant_id: int, phones: list[str]) -> int:
    """Pausa o agente para todos os telefones informados. Retorna nº pausados."""
    if not phones:
        return 0
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO agent_paused (tenant_id, phone) VALUES (?, ?)",
            [(tenant_id, p) for p in phones if p]
        )
    return len([p for p in phones if p])


def resume_all_agents(tenant_id: int) -> int:
    """Remove TODAS as pausas do tenant. Retorna nº de pausas removidas."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM agent_paused WHERE tenant_id = ?", (tenant_id,))
        return cur.rowcount or 0


def clear_conversation(tenant_id: int, phone: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM conversations WHERE tenant_id = ? AND phone = ?", (tenant_id, phone))


# ── Appointments ───────────────────────────────────────────────────────────────

def get_appointments_by_phone(tenant_id: int, phone: str, now_iso: str | None = None) -> list[dict]:
    # Appointments are stored in Brasília time (naive). Use now_iso passed from caller
    # to avoid comparing against SQLite's datetime('now') which is UTC.
    # IMPORTANTE: consideramos "sessão atual" qualquer agendamento iniciado nos
    # últimos 90 minutos — assim o agente continua reconhecendo a consulta de hoje
    # mesmo se o paciente mandar mensagem 20-30min depois do horário marcado.
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    if now_iso is None:
        now_dt = datetime.now(ZoneInfo("America/Sao_Paulo")).replace(tzinfo=None)
    else:
        try:
            now_dt = datetime.fromisoformat(now_iso)
        except Exception:
            now_dt = datetime.now(ZoneInfo("America/Sao_Paulo")).replace(tzinfo=None)
    threshold = (now_dt - timedelta(minutes=90)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM appointments
               WHERE tenant_id = ? AND phone = ? AND scheduled_at >= ?
               ORDER BY scheduled_at""",
            (tenant_id, phone, threshold),
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
    """Atualiza data/hora da consulta e reseta flags de confirmação (nova data = pendente)."""
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE appointments
               SET scheduled_at = ?, confirmed = 0, confirmation_sent = 0, followup_sent = 0
               WHERE id = ? AND tenant_id = ?""",
            (scheduled_at.isoformat(), appointment_id, tenant_id),
        )
        return cur.rowcount > 0


def rename_patient(tenant_id: int, appointment_id: int, new_name: str, apply_all: bool = False) -> int:
    """Renomeia o paciente em uma consulta (ou em todas as consultas com o mesmo telefone, se apply_all=True).
    Retorna o número de linhas atualizadas."""
    new_name = (new_name or "").strip()
    if not new_name:
        return 0
    with get_conn() as conn:
        if apply_all:
            # Pega o telefone da consulta-alvo e atualiza todas as consultas do mesmo telefone
            row = conn.execute(
                "SELECT phone FROM appointments WHERE id = ? AND tenant_id = ?",
                (appointment_id, tenant_id),
            ).fetchone()
            if not row:
                return 0
            cur = conn.execute(
                "UPDATE appointments SET patient_name = ? WHERE tenant_id = ? AND phone = ?",
                (new_name, tenant_id, row["phone"]),
            )
        else:
            cur = conn.execute(
                "UPDATE appointments SET patient_name = ? WHERE id = ? AND tenant_id = ?",
                (new_name, appointment_id, tenant_id),
            )
        return cur.rowcount


def confirm_appointment(tenant_id: int, appointment_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE appointments SET confirmed = 1 WHERE id = ? AND tenant_id = ?",
            (appointment_id, tenant_id),
        )
        return cur.rowcount > 0


ATTENDANCE_VALUES = {"pending", "attended", "missed_no_notice", "missed_with_notice"}


def set_attendance(tenant_id: int, appointment_id: int, status: str) -> bool:
    """Atualiza status de comparecimento. Valores válidos:
    - pending: ainda não passou / não marcado
    - attended: compareceu (cobra)
    - missed_no_notice: faltou sem aviso (COBRA)
    - missed_with_notice: não compareceu com aviso (NÃO COBRA)
    """
    if status not in ATTENDANCE_VALUES:
        return False
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE appointments SET attendance = ? WHERE id = ? AND tenant_id = ?",
            (status, appointment_id, tenant_id),
        )
        return cur.rowcount > 0


def rename_patient_by_phone(tenant_id: int, phone: str, new_name: str) -> int:
    """Atualiza o nome de todos os agendamentos e do cadastro do paciente.
    Usado quando o contato apareceu inicialmente só com número.
    Funciona mesmo para contatos que só têm conversas (sem agendamentos).
    """
    new_name = (new_name or "").strip()
    if not new_name or not phone:
        return 0
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE appointments SET patient_name = ? WHERE tenant_id = ? AND phone = ?",
            (new_name, tenant_id, phone),
        )
        # Sempre faz upsert na tabela patients — garante que o nome fica salvo
        # mesmo para contatos que só aparecem em conversas (sem agendamentos)
        conn.execute("""
            INSERT INTO patients (tenant_id, phone, name)
            VALUES (?, ?, ?)
            ON CONFLICT(tenant_id, phone) DO UPDATE SET name = excluded.name
        """, (tenant_id, phone, new_name))
        return cur.rowcount or 0


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
    """Retorna consultas cuja janela de 23-25h antes da sessão está aberta.

    Janela estrita: a confirmação só sai quando faltam entre 23h e 25h
    para a consulta. Como o scheduler roda a cada 30 min (entre 8h-21h),
    qualquer sessão com janela sobreposta a esse intervalo é capturada
    exatamente uma vez (flag confirmation_sent=0 evita duplicidade).
    """
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("America/Sao_Paulo")
    now_br = _dt.now(_TZ).replace(tzinfo=None)  # naive, mesmo formato do scheduled_at
    window_start = (now_br + _td(hours=23)).isoformat(timespec="seconds")
    window_end   = (now_br + _td(hours=25)).isoformat(timespec="seconds")
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM appointments
               WHERE tenant_id = ?
                 AND confirmation_sent = 0
                 AND cancelled = 0
                 AND scheduled_at > ?
                 AND scheduled_at <= ?
                 AND NOT EXISTS (
                     SELECT 1 FROM agent_paused ap
                     WHERE ap.tenant_id = appointments.tenant_id
                       AND ap.phone = appointments.phone
                 )
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
                 AND NOT EXISTS (
                     SELECT 1 FROM agent_paused ap
                     WHERE ap.tenant_id = appointments.tenant_id
                       AND ap.phone = appointments.phone
                 )
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


def has_conflict(tenant_id: int, dt: datetime, duration_min: int, exclude_id: int | None = None) -> bool:
    """Detecta sobreposição real: True se `dt` cair dentro de [b - duration, b + duration]
    de qualquer consulta existente (exceto a própria, via exclude_id).
    """
    from datetime import datetime as _dt
    with get_conn() as conn:
        query = "SELECT id, scheduled_at FROM appointments WHERE tenant_id = ?"
        params = [tenant_id]
        if exclude_id is not None:
            query += " AND id != ?"
            params.append(exclude_id)
        rows = conn.execute(query, params).fetchall()
    for r in rows:
        try:
            b = _dt.fromisoformat(r["scheduled_at"])
        except Exception:
            continue
        if abs((dt - b).total_seconds()) < duration_min * 60:
            return True
    return False


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
    """Sessões que entram no faturamento do mês.

    Critério: confirmadas, ocorreram dentro do mês, e:
    - attendance != 'missed_with_notice'  (não compareceu com aviso → NÃO cobra)
    - cancelled = 0
    'attended', 'missed_no_notice' e 'pending' (default) entram no cálculo.
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM appointments
            WHERE tenant_id = ? AND phone = ?
              AND confirmed = 1
              AND cancelled = 0
              AND COALESCE(attendance, 'pending') != 'missed_with_notice'
              AND scheduled_at >= ? AND scheduled_at < ?
              AND scheduled_at <= ?
            ORDER BY scheduled_at
        """, (tenant_id, phone, month_start, month_end, now_str)).fetchall()
    return [dict(r) for r in rows]


def get_session_counts_by_month(tenant_id: int, month_start: str, month_end: str) -> dict[str, int]:
    """Quantidade de sessões 'efetivas' por telefone no mês.

    Conta: attended + missed_no_notice + pending (ainda não marcadas).
    Exclui: missed_with_notice + cancelled.
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT phone, COUNT(*) AS n FROM appointments
            WHERE tenant_id = ?
              AND scheduled_at >= ? AND scheduled_at < ?
              AND cancelled = 0
              AND COALESCE(attendance, 'pending') != 'missed_with_notice'
            GROUP BY phone
        """, (tenant_id, month_start, month_end)).fetchall()
    return {r["phone"]: r["n"] for r in rows}


def get_full_patient_history(tenant_id: int, phone: str) -> list[dict]:
    """Histórico completo de um paciente (todas as consultas, ordem decrescente)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM appointments
               WHERE tenant_id = ? AND phone = ?
               ORDER BY scheduled_at DESC""",
            (tenant_id, phone),
        ).fetchall()
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


def get_dashboard_stats(tenant_id: int, month_start: str, month_end: str, now_str: str) -> dict:
    """Indicadores do mês: sessões, receita, taxa de presença, pacientes ativos."""
    with get_conn() as conn:
        total = conn.execute("""SELECT COUNT(*) FROM appointments
            WHERE tenant_id=? AND scheduled_at>=? AND scheduled_at<? AND cancelled=0
        """, (tenant_id, month_start, month_end)).fetchone()[0]

        attended = conn.execute("""SELECT COUNT(*) FROM appointments
            WHERE tenant_id=? AND scheduled_at>=? AND scheduled_at<? AND cancelled=0
              AND attendance='attended'
        """, (tenant_id, month_start, month_end)).fetchone()[0]

        missed_no = conn.execute("""SELECT COUNT(*) FROM appointments
            WHERE tenant_id=? AND scheduled_at>=? AND scheduled_at<? AND cancelled=0
              AND attendance='missed_no_notice'
        """, (tenant_id, month_start, month_end)).fetchone()[0]

        missed_with = conn.execute("""SELECT COUNT(*) FROM appointments
            WHERE tenant_id=? AND scheduled_at>=? AND scheduled_at<? AND cancelled=0
              AND attendance='missed_with_notice'
        """, (tenant_id, month_start, month_end)).fetchone()[0]

        cancelled_n = conn.execute("""SELECT COUNT(*) FROM appointments
            WHERE tenant_id=? AND scheduled_at>=? AND scheduled_at<? AND cancelled=1
        """, (tenant_id, month_start, month_end)).fetchone()[0]

        active_patients = conn.execute("""SELECT COUNT(DISTINCT phone) FROM appointments
            WHERE tenant_id=? AND scheduled_at>=? AND scheduled_at<? AND cancelled=0
        """, (tenant_id, month_start, month_end)).fetchone()[0]

        revenue_rows = conn.execute("""
            SELECT a.phone, COUNT(*) as sessions FROM appointments a
            WHERE a.tenant_id=? AND a.scheduled_at>=? AND a.scheduled_at<?
              AND a.cancelled=0
              AND COALESCE(a.attendance,'pending') != 'missed_with_notice'
              AND a.scheduled_at <= ?
            GROUP BY a.phone
        """, (tenant_id, month_start, month_end, now_str)).fetchall()

        revenue = 0.0
        for row in revenue_rows:
            pr = conn.execute(
                "SELECT session_price FROM patients WHERE tenant_id=? AND phone=?",
                (tenant_id, row["phone"])
            ).fetchone()
            if pr and pr["session_price"]:
                revenue += pr["session_price"] * row["sessions"]

        # Distribuição por dia da semana — strftime('%w')=0 Dom … 6 Sáb → Seg=0…Dom=6
        wd_rows = conn.execute("""
            SELECT CAST(strftime('%w', scheduled_at) AS INTEGER) as wd, COUNT(*) as n
            FROM appointments
            WHERE tenant_id=? AND scheduled_at>=? AND scheduled_at<? AND cancelled=0
            GROUP BY wd
        """, (tenant_id, month_start, month_end)).fetchall()
        wd_map = {(r["wd"] + 6) % 7: r["n"] for r in wd_rows}
        weekdays = [wd_map.get(i, 0) for i in range(7)]

    pending = max(0, total - attended - missed_no - missed_with - cancelled_n)
    return {
        "total": total, "attended": attended,
        "missed_no_notice": missed_no, "missed_with_notice": missed_with,
        "cancelled": cancelled_n, "pending": pending,
        "active_patients": active_patients,
        "revenue": round(revenue, 2),
        "weekdays": weekdays,
    }


# ════════════════════════════════════════════════════════════════════════════
# Admin (autenticação)
# ════════════════════════════════════════════════════════════════════════════

import hashlib as _hashlib
import secrets as _secrets
from datetime import datetime as _datetime, timedelta as _timedelta


def admin_verify_login(username_or_email: str, password: str) -> dict | None:
    """Valida credenciais. Retorna o admin (sem senha) ou None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM admin_users WHERE username = ? OR email = ? LIMIT 1",
            (username_or_email, username_or_email),
        ).fetchone()
    if not row:
        return None
    expected = _hashlib.pbkdf2_hmac("sha256", password.encode(), row["salt"].encode(), 100_000).hex()
    if not _secrets.compare_digest(expected, row["password_hash"]):
        return None
    return {"id": row["id"], "username": row["username"], "email": row["email"]}


def admin_create_session(username: str, days: int = 7) -> str:
    """Cria token de sessão e retorna o token."""
    token = _secrets.token_urlsafe(32)
    expires = (_datetime.utcnow() + _timedelta(days=days)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO admin_sessions (token, username, expires_at) VALUES (?, ?, ?)",
            (token, username, expires),
        )
    return token


def admin_get_session(token: str) -> dict | None:
    """Retorna sessão se válida e não expirada, senão None."""
    if not token:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM admin_sessions WHERE token = ?", (token,)
        ).fetchone()
    if not row:
        return None
    try:
        if _datetime.fromisoformat(row["expires_at"]) < _datetime.utcnow():
            return None
    except Exception:
        return None
    return dict(row)


def admin_delete_session(token: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))


def admin_change_password(username: str, new_password: str) -> bool:
    salt = _secrets.token_hex(16)
    pw_hash = _hashlib.pbkdf2_hmac("sha256", new_password.encode(), salt.encode(), 100_000).hex()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE admin_users SET password_hash = ?, salt = ? WHERE username = ?",
            (pw_hash, salt, username),
        )
        return cur.rowcount > 0


# ════════════════════════════════════════════════════════════════════════════
# Landing page tracking
# ════════════════════════════════════════════════════════════════════════════

def record_landing_view(ip: str = "", user_agent: str = "", referrer: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO landing_views (ip, user_agent, referrer) VALUES (?, ?, ?)",
            (ip[:64], user_agent[:512], referrer[:512]),
        )


def count_landing_views(days: int | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM landing_views"
    params = ()
    if days:
        sql += " WHERE viewed_at >= datetime('now', ?)"
        params = (f"-{int(days)} days",)
    with get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
    return int(row["n"] or 0)


# ════════════════════════════════════════════════════════════════════════════
# CMS / Conteúdo do site (depoimentos editáveis, etc)
# ════════════════════════════════════════════════════════════════════════════

def get_site_content(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM site_content WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_site_content(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO site_content (key, value, updated_at) VALUES (?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')""",
            (key, value),
        )


# ════════════════════════════════════════════════════════════════════════════
# Admin stats — métricas agregadas
# ════════════════════════════════════════════════════════════════════════════

# Preços dos planos (sincronizados com stripe_service.PLANS)
_PLAN_MONTHLY_VALUE = {"mensal": 199.0, "semestral": 169.0, "anual": 149.0}


def admin_stats_overview() -> dict:
    """Retorna KPIs do painel: tenants, MRR, vendas, conversão."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, plan, free_until, created_at FROM tenants WHERE active = 1"
        ).fetchall()

    total = len(rows)
    active = sum(1 for r in rows if r["status"] == "active")
    suspended = sum(1 for r in rows if r["status"] == "suspended")
    pending = sum(1 for r in rows if r["status"] == "pending_payment")

    # MRR = soma de active * valor mensal-equivalente do plano
    mrr = 0.0
    by_plan = {"mensal": 0, "semestral": 0, "anual": 0}
    for r in rows:
        if r["status"] != "active":
            continue
        plan = r["plan"] or "mensal"
        by_plan[plan] = by_plan.get(plan, 0) + 1
        mrr += _PLAN_MONTHLY_VALUE.get(plan, 199.0)

    # Cadastros nas últimas 24h e 7d
    new_24h = sum(1 for r in rows if r["created_at"] and r["created_at"] >= (_datetime.utcnow() - _timedelta(days=1)).isoformat())
    new_7d  = sum(1 for r in rows if r["created_at"] and r["created_at"] >= (_datetime.utcnow() - _timedelta(days=7)).isoformat())

    views_total = count_landing_views()
    views_7d = count_landing_views(7)
    views_30d = count_landing_views(30)

    return {
        "tenants": {"total": total, "active": active, "suspended": suspended, "pending_payment": pending},
        "mrr": round(mrr, 2),
        "by_plan": by_plan,
        "signups": {"last_24h": new_24h, "last_7d": new_7d},
        "landing_views": {"total": views_total, "last_7d": views_7d, "last_30d": views_30d},
        "conversion_30d": round((active / views_30d * 100), 2) if views_30d else 0.0,
    }


def admin_list_subscriptions() -> list[dict]:
    """Lista todas as assinaturas ativas com plano, vencimento, etc."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, slug, name, full_name, email, phone, psychologist_name,
                   plan, status, plan_expires_at, free_until, created_at,
                   stripe_subscription_id, mp_subscription_id
            FROM tenants
            WHERE active = 1 AND status = 'active'
            ORDER BY created_at DESC
        """).fetchall()
    return [dict(r) for r in rows]


def admin_list_abandoned_carts(hours_min: int = 1) -> list[dict]:
    """Pessoas que criaram conta mas não pagaram (status pending_payment, criados há > hours_min)."""
    cutoff = (_datetime.utcnow() - _timedelta(hours=hours_min)).isoformat()
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, slug, name, full_name, email, phone, cpf_cnpj,
                   plan, setup_token, created_at
            FROM tenants
            WHERE active = 1 AND status = 'pending_payment' AND created_at <= ?
            ORDER BY created_at DESC
        """, (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def admin_list_all_tenants() -> list[dict]:
    """Lista completa de tenants para gestão."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, slug, name, full_name, email, phone, cpf_cnpj,
                   psychologist_name, plan, status, plan_expires_at, free_until,
                   billing_city, billing_state, created_at,
                   stripe_subscription_id, mp_subscription_id, dashboard_token, setup_token
            FROM tenants
            WHERE active = 1
            ORDER BY created_at DESC
        """).fetchall()
    return [dict(r) for r in rows]


def admin_get_tenant_full(slug: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE slug = ?", (slug,)).fetchone()
    return dict(row) if row else None


# ════════════════════════════════════════════════════════════════════════════
# Audit log + login attempts (P1 security)
# ════════════════════════════════════════════════════════════════════════════

def audit_log(action: str, actor: str = "", target: str = "", ip: str = "", details: str = ""):
    """Registra ação sensível para auditoria. Falha silenciosamente — nunca quebra a request."""
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (actor, action, target, ip, details) VALUES (?, ?, ?, ?, ?)",
                (actor[:64], action[:64], target[:128], ip[:64], details[:512]),
            )
    except Exception:
        pass


def audit_list(limit: int = 100) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def record_login_attempt(username: str, ip: str, success: bool):
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO login_attempts (username, ip, success) VALUES (?, ?, ?)",
                (username[:64], ip[:64], 1 if success else 0),
            )
    except Exception:
        pass


def count_recent_failed_logins(username: str, ip: str, minutes: int = 15) -> int:
    """Conta falhas recentes para (username) OU (ip)."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM login_attempts
                   WHERE success = 0
                     AND attempted_at >= datetime('now', ?)
                     AND (username = ? OR ip = ?)""",
                (f"-{minutes} minutes", username, ip),
            ).fetchone()
            return int(row["n"]) if row else 0
    except Exception:
        return 0


def is_account_locked(username: str, ip: str, threshold: int = 8, minutes: int = 15) -> bool:
    return count_recent_failed_logins(username, ip, minutes) >= threshold


def clear_login_attempts(username: str):
    """Limpa tentativas após login bem-sucedido."""
    try:
        with get_conn() as conn:
            conn.execute("DELETE FROM login_attempts WHERE username = ? AND success = 0", (username,))
    except Exception:
        pass


def admin_get_totp(username: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT totp_secret, totp_enabled FROM admin_users WHERE username = ?",
            (username,)
        ).fetchone()
    if not row:
        return None
    return {"secret": row["totp_secret"] or "", "enabled": bool(row["totp_enabled"])}


def admin_set_totp(username: str, secret: str, enabled: bool) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE admin_users SET totp_secret = ?, totp_enabled = ? WHERE username = ?",
            (secret, 1 if enabled else 0, username),
        )
        return cur.rowcount > 0


def admin_disable_totp(username: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE admin_users SET totp_secret = '', totp_enabled = 0 WHERE username = ?",
            (username,),
        )
        return cur.rowcount > 0
