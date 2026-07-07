from __future__ import annotations
import os
import sqlite3
from datetime import datetime
from contextlib import contextmanager

import config as _cfg
import crypto
DB_PATH = os.path.join(_cfg.DATA_DIR, "consultorio.db")

# Campos de tenant cifrados em repouso (credenciais + chave PIX). Ver crypto.py.
_ENCRYPTED_TENANT_FIELDS = (
    "evolution_key",         # token Z-API / Evolution
    "evolution_url",         # Z-API: Client-Token (header de segurança)
    "twilio_token",          # auth token Twilio
    "google_refresh_token",  # OAuth Google Calendar
    "caldav_password",       # senha de app CalDAV
    "pix_key",               # chave PIX
)


def _decrypt_tenant(d: "dict | None") -> "dict | None":
    """Decifra os campos sensíveis de um dict de tenant (in-place). Retrocompatível:
    valores em texto puro (legado) passam direto."""
    if not d:
        return d
    for f in _ENCRYPTED_TENANT_FIELDS:
        if d.get(f) is not None:
            d[f] = crypto.decrypt(d[f])
    return d


def init_db():
    with get_conn() as conn:
        # WAL é persistente no arquivo: se um deploy anterior ligou WAL, o banco
        # continua em WAL mesmo sem o PRAGMA. Volta explicitamente p/ DELETE (o
        # modo conhecido-bom no volume em rede do Railway) e remove -wal/-shm.
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
        except Exception:
            pass
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

            -- Contas de infraestrutura do operador (Railway, Anthropic, domínio…)
            -- cadastradas manualmente, pois o app não enxerga vencimentos de 3os.
            CREATE TABLE IF NOT EXISTS op_bills (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                label       TEXT NOT NULL,                 -- ex: "Railway", "Anthropic", "Domínio"
                amount      REAL DEFAULT 0,                -- valor (informativo)
                due_date    TEXT NOT NULL,                 -- próximo vencimento YYYY-MM-DD
                recurrence  TEXT NOT NULL DEFAULT 'monthly', -- none | monthly | yearly
                active      INTEGER NOT NULL DEFAULT 1,
                notes       TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now'))
            );

            -- Log idempotente de lembretes de vencimento enviados.
            -- kind: 'op_bill' (conta do operador) | 'zapi' (instância do consultório)
            -- ref_id: id da op_bill OU id do tenant
            -- due_date: vencimento a que o lembrete se refere (chave de idempotência)
            CREATE TABLE IF NOT EXISTS bill_reminders_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                kind      TEXT NOT NULL,
                ref_id    INTEGER NOT NULL,
                due_date  TEXT NOT NULL,
                channel   TEXT DEFAULT '',
                sent_at   TEXT DEFAULT (datetime('now')),
                UNIQUE(kind, ref_id, due_date)
            );

            -- Saúde da instância de WhatsApp (Z-API) por consultório.
            -- connected: 1 online | 0 caiu | NULL desconhecido (falha ao checar)
            CREATE TABLE IF NOT EXISTS instance_health (
                tenant_id    INTEGER PRIMARY KEY,
                connected    INTEGER,
                fail_count   INTEGER NOT NULL DEFAULT 0,
                down_since   TEXT,
                alerted_at   TEXT,
                last_checked TEXT,
                last_error   TEXT DEFAULT ''
            );

            -- Ajuste manual do valor total cobrado de um paciente num mês
            -- específico (ex.: desconto combinado ou complemento de sessão).
            -- Quando existe override para (tenant, phone, month), ele SUBSTITUI
            -- o cálculo padrão (nº de sessões × valor da sessão) na cobrança.
            CREATE TABLE IF NOT EXISTS billing_overrides (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id    INTEGER NOT NULL,
                phone        TEXT NOT NULL,
                month        TEXT NOT NULL,          -- 'YYYY-MM'
                total_amount REAL NOT NULL DEFAULT 0,
                note         TEXT DEFAULT '',
                updated_at   TEXT DEFAULT (datetime('now')),
                UNIQUE(tenant_id, phone, month)
            );

            -- Cobranças AVULSAS (manuais): sessão extra fora da agenda, ou
            -- paciente que nunca falou pelo WhatsApp profissional. Entram na
            -- prévia/total e podem ser enviadas (se houver telefone). Cada
            -- registro pertence a um mês e é enviado no máximo UMA vez (sent_at).
            CREATE TABLE IF NOT EXISTS billing_manual_entries (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id      INTEGER NOT NULL,
                month          TEXT NOT NULL,          -- 'YYYY-MM'
                patient_name   TEXT NOT NULL,
                phone          TEXT DEFAULT '',        -- vazio = só registro, não envia
                sessions_count INTEGER DEFAULT 1,
                total_amount   REAL NOT NULL DEFAULT 0,
                note           TEXT DEFAULT '',
                sent_at        TEXT,                   -- preenchido ao enviar via WhatsApp
                created_at     TEXT DEFAULT (datetime('now'))
            );

            -- Controle de PAGAMENTO por paciente/mês (independente do envio da
            -- cobrança). A presença de uma linha = pago; ausência = não pago.
            -- Puramente informativo/gerencial: NÃO afeta cálculo nem disparo.
            CREATE TABLE IF NOT EXISTS billing_payments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id  INTEGER NOT NULL,
                phone      TEXT NOT NULL,
                month      TEXT NOT NULL,          -- 'YYYY-MM'
                paid_at    TEXT DEFAULT (datetime('now')),
                UNIQUE(tenant_id, phone, month)
            );

            -- Comprovantes recebidos pelo WhatsApp (imagem/documento/PIX). Serve
            -- só como SINALIZAÇÃO para a psicóloga confirmar o pagamento na tela
            -- de Cobranças. NÃO marca como pago automaticamente nem confia na
            -- imagem — a confirmação continua sendo humana (botão "✓ pago").
            CREATE TABLE IF NOT EXISTS billing_receipts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id    INTEGER NOT NULL,
                phone        TEXT NOT NULL,
                month        TEXT NOT NULL,          -- 'YYYY-MM'
                kind         TEXT DEFAULT '',        -- imagem | documento | pix
                received_at  TEXT DEFAULT (datetime('now')),
                dismissed_at TEXT DEFAULT NULL,      -- dispensado sem confirmar
                UNIQUE(tenant_id, phone, month)
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
            # Bloqueio de horas por dia da semana — JSON tipo {"1":[17],"2":[17,18]}
            # (chaves = weekday 0=Seg…6=Dom; valores = lista de horas bloqueadas só nesse dia)
            "ALTER TABLE tenants ADD COLUMN blocked_hours_by_day TEXT DEFAULT ''",
            # Vencimento da instância Z-API do consultório (responsabilidade da
            # psicóloga) — YYYY-MM-DD. NULL = não cadastrado / não avisa.
            "ALTER TABLE tenants ADD COLUMN zapi_expires_at TEXT DEFAULT NULL",
            # Pausar envio de cobrança — global (tenant) e por paciente.
            # Separado de agent_paused: pausar o chat ≠ não cobrar.
            "ALTER TABLE patients ADD COLUMN billing_paused INTEGER DEFAULT 0",
            "ALTER TABLE tenants ADD COLUMN billing_paused INTEGER DEFAULT 0",
            # Controle de pagamento das cobranças avulsas (NULL = não pago).
            "ALTER TABLE billing_manual_entries ADD COLUMN paid_at TEXT DEFAULT NULL",
            # ── Generalização multi-segmento (Track B) ──
            # segment='psicologia' preserva 100% o comportamento atual (prompt e
            # textos idênticos). Outros valores ativam o caminho genérico usando
            # os rótulos abaixo. Rótulos vazios → fallback genérico sensato.
            #   professional_label: como o profissional é chamado (ex.: "Dentista")
            #   client_noun: como o cliente é chamado (ex.: "cliente", "paciente")
            #   service_noun: o serviço prestado (ex.: "atendimento", "consulta")
            #   business_type: tipo do negócio (ex.: "clínica", "escritório")
            "ALTER TABLE tenants ADD COLUMN segment TEXT DEFAULT 'psicologia'",
            "ALTER TABLE tenants ADD COLUMN professional_label TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN client_noun TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN service_noun TEXT DEFAULT ''",
            "ALTER TABLE tenants ADD COLUMN business_type TEXT DEFAULT ''",
        ]
        for sql in migrations:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # coluna já existe

        # ── Limpeza one-shot: remove saudação "Boa tarde..." inicial dos
        # templates de cobrança personalizados (passa a iniciar pelo nome). ──
        try:
            rows = conn.execute(
                "SELECT id, billing_msg_template FROM tenants "
                "WHERE billing_msg_template IS NOT NULL AND billing_msg_template != ''"
            ).fetchall()
            import re as _re
            _BAD_PREFIX = _re.compile(
                r"^\s*(boa\s*tarde+e*|bom\s*dia+a*|boa\s*noite+e*|ol[áa]+|oi+)"
                r"[^\n]*\n+",
                _re.IGNORECASE,
            )
            for row in rows:
                tpl = row["billing_msg_template"]
                new_tpl = _BAD_PREFIX.sub("", tpl, count=1)
                if new_tpl != tpl:
                    conn.execute(
                        "UPDATE tenants SET billing_msg_template=? WHERE id=?",
                        (new_tpl, row["id"]),
                    )
        except Exception:
            pass  # se algo falhar, não quebra o boot

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
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # busy_timeout: aguarda o lock liberar (até 30s) em vez de estourar
    # "database is locked" na hora. Seguro em qualquer filesystem.
    # NÃO usar WAL aqui: o volume do Railway é storage em rede e o arquivo
    # -shm (shared memory / mmap) do WAL degrada MUITO nesse tipo de FS
    # (healthz foi de 0,5s p/ 6-21s quando WAL foi ligado). Modo padrão
    # ('delete') é o conhecido-bom; escritas normais são de milissegundos.
    try:
        conn.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass  # fail-open
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
    return _decrypt_tenant(dict(row)) if row else None


def get_tenant_by_id(tenant_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    return _decrypt_tenant(dict(row)) if row else None


def get_tenant_by_token(token: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE dashboard_token = ? AND active = 1", (token,)
        ).fetchone()
    return _decrypt_tenant(dict(row)) if row else None


def get_tenant_by_setup_token(token: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE setup_token = ? AND active = 1", (token,)
        ).fetchone()
    return _decrypt_tenant(dict(row)) if row else None


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
    return [_decrypt_tenant(dict(r)) for r in rows]


def update_tenant(slug: str, **fields) -> bool:
    allowed = {
        "name", "psychologist_name", "working_hours_start", "working_hours_end",
        "session_minutes", "whatsapp_provider", "evolution_url", "evolution_key",
        "evolution_instance", "twilio_sid", "twilio_token", "twilio_from", "active",
        "dashboard_token", "setup_token", "google_refresh_token", "google_calendar_id",
        "email", "status", "stripe_customer_id", "stripe_subscription_id", "mp_subscription_id",
        "pix_key", "pix_name",
        "working_days", "blocked_hours", "blocked_hours_by_day", "blocked_dates", "confirmation_hour", "psychologist_phone", "plan",
        "confirmation_msg_template", "followup_msg_template", "billing_msg_template",
        "free_until", "plan_expires_at", "zapi_expires_at",
        "caldav_url", "caldav_username", "caldav_password",
        "full_name", "cpf_cnpj", "phone",
        "billing_zip", "billing_address", "billing_number", "billing_complement",
        "billing_neighborhood", "billing_city", "billing_state",
        "accepted_terms_at", "accepted_terms_version",
        # Generalização multi-segmento (Track B)
        "segment", "professional_label", "client_noun", "service_noun", "business_type",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    # Cifra credenciais/PIX antes de gravar (transparente; ver crypto.py).
    for k in _ENCRYPTED_TENANT_FIELDS:
        if k in updates and isinstance(updates[k], str):
            updates[k] = crypto.encrypt(updates[k])
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [slug]
    with get_conn() as conn:
        cur = conn.execute(f"UPDATE tenants SET {set_clause} WHERE slug = ?", values)
        return cur.rowcount > 0


def encrypt_existing_data(include_conversations: bool = False) -> dict:
    """Migra dados legados (texto puro) para cifrado em repouso. Idempotente —
    pula valores já cifrados. Requer FIELD_ENCRYPTION_KEY ativa (ver crypto.py).

    Credenciais/PIX dos tenants são migrados sempre; o conteúdo das conversas
    (volumoso) só quando include_conversations=True, em lotes de 500."""
    tenants_updated = 0
    fields_encrypted = 0
    cols = ", ".join(_ENCRYPTED_TENANT_FIELDS)
    with get_conn() as conn:
        rows = conn.execute(f"SELECT id, {cols} FROM tenants").fetchall()
        for r in rows:
            sets = {}
            for f in _ENCRYPTED_TENANT_FIELDS:
                v = r[f]
                if isinstance(v, str) and v and not crypto.is_encrypted(v):
                    sets[f] = crypto.encrypt(v)
            if sets:
                clause = ", ".join(f"{k} = ?" for k in sets)
                conn.execute(
                    f"UPDATE tenants SET {clause} WHERE id = ?",
                    list(sets.values()) + [r["id"]],
                )
                tenants_updated += 1
                fields_encrypted += len(sets)

    conversations_encrypted = 0
    if include_conversations:
        last_id = 0
        while True:
            with get_conn() as conn:
                batch = conn.execute(
                    "SELECT id, content FROM conversations WHERE id > ? ORDER BY id LIMIT 500",
                    (last_id,),
                ).fetchall()
                if not batch:
                    break
                for cr in batch:
                    last_id = cr["id"]
                    v = cr["content"]
                    if isinstance(v, str) and v and not crypto.is_encrypted(v):
                        conn.execute(
                            "UPDATE conversations SET content = ? WHERE id = ?",
                            (crypto.encrypt(v), cr["id"]),
                        )
                        conversations_encrypted += 1
    return {
        "tenants_updated": tenants_updated,
        "fields_encrypted": fields_encrypted,
        "conversations_encrypted": conversations_encrypted,
    }


# ── Conversations ──────────────────────────────────────────────────────────────

def get_conversation_history(tenant_id: int, phone: str, limit: int = 10) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT role, content FROM conversations
               WHERE tenant_id = ? AND phone = ?
               ORDER BY created_at DESC LIMIT ?""",
            (tenant_id, phone, limit),
        ).fetchall()
    return [{"role": r["role"], "content": crypto.decrypt(r["content"])} for r in reversed(rows)]


def save_message(tenant_id: int, phone: str, role: str, content: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO conversations (tenant_id, phone, role, content) VALUES (?, ?, ?, ?)",
            (tenant_id, phone, role, crypto.encrypt(content)),
        )


def _norm_digits(phone: str) -> str:
    """Remove tudo que não for dígito — garante consistência na tabela agent_paused."""
    return "".join(c for c in (phone or "") if c.isdigit())


def is_agent_paused(tenant_id: int, phone: str) -> bool:
    """Verifica pause tolerando divergência de DDI (55) e do dígito 9
    do celular brasileiro entre o que foi gravado e o que chega no webhook.

    Exemplo: usuária pausou '41988667599' pelo painel mas o Z-API entrega
    '5541988667599'. Antes, isso causava o agente a continuar respondendo
    um paciente 'pausado'. Agora as variantes plausíveis são todas
    consultadas com IN (...).
    """
    p = _norm_digits(phone)
    if not p:
        return False
    variantes = {p}
    # Sem DDI 55 → adiciona com
    if not p.startswith("55") and len(p) >= 10:
        variantes.add("55" + p)
    # Com DDI 55 → adiciona sem
    if p.startswith("55") and len(p) >= 12:
        variantes.add(p[2:])
    # Variantes com/sem o "9" extra do celular (após DDD)
    extra = set()
    for v in list(variantes):
        if v.startswith("55") and len(v) == 13 and v[4] == "9":
            extra.add(v[:4] + v[5:])  # remove o 9
        elif v.startswith("55") and len(v) == 12:
            extra.add(v[:4] + "9" + v[4:])  # adiciona o 9
        elif len(v) == 11 and v[2] == "9":
            extra.add(v[:2] + v[3:])
        elif len(v) == 10:
            extra.add(v[:2] + "9" + v[2:])
    variantes |= extra
    placeholders = ",".join("?" * len(variantes))
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT 1 FROM agent_paused WHERE tenant_id = ? AND phone IN ({placeholders})",
            (tenant_id, *variantes),
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


def _phone_variants(phone: str) -> set[str]:
    """Variantes plausíveis de um número BR (com/sem DDI 55 e dígito 9 extra).
    Usado para casar registros mesmo quando gravados em formatos diferentes
    (ex.: conversa veio '5541988667599' mas pause foi gravado '41988667599').
    """
    p = _norm_digits(phone)
    variantes: set[str] = set()
    if not p:
        return variantes
    variantes.add(p)
    if not p.startswith("55") and len(p) >= 10:
        variantes.add("55" + p)
    if p.startswith("55") and len(p) >= 12:
        variantes.add(p[2:])
    extra: set[str] = set()
    for v in list(variantes):
        if v.startswith("55") and len(v) == 13 and v[4] == "9":
            extra.add(v[:4] + v[5:])
        elif v.startswith("55") and len(v) == 12:
            extra.add(v[:4] + "9" + v[4:])
        elif len(v) == 11 and v[2] == "9":
            extra.add(v[:2] + v[3:])
        elif len(v) == 10:
            extra.add(v[:2] + "9" + v[2:])
    variantes |= extra
    return variantes


def delete_patient_completely(tenant_id: int, phone: str) -> dict:
    """Exclui DEFINITIVAMENTE um paciente: conversas, agendamentos, cadastro,
    pausas e overrides de cobrança. PRESERVA billing_logs (histórico financeiro
    já enviado — registro contábil). Caso de desistência / LGPD.

    Casa tanto o telefone exato (como gravado em conversations/appointments/
    patients) quanto as variantes normalizadas (agent_paused guarda dígitos).
    Retorna contagem de linhas removidas por tabela.
    """
    variants = _phone_variants(phone)
    # Inclui o phone cru exatamente como veio (caso não normalize p/ dígitos)
    all_phones = set(variants)
    if phone:
        all_phones.add(phone)
    if not all_phones:
        return {"conversations": 0, "appointments": 0, "patients": 0,
                "agent_paused": 0, "billing_overrides": 0}
    ph = ",".join("?" * len(all_phones))
    params = (tenant_id, *all_phones)
    out: dict[str, int] = {}
    with get_conn() as conn:
        for table in ("conversations", "appointments", "patients",
                      "agent_paused", "billing_overrides"):
            cur = conn.execute(
                f"DELETE FROM {table} WHERE tenant_id = ? AND phone IN ({ph})",
                params,
            )
            out[table] = cur.rowcount or 0
    return out


# ── Pausa de cobrança (global / por paciente) ──────────────────────────────────

def set_tenant_billing_paused(tenant_id: int, paused: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE tenants SET billing_paused = ? WHERE id = ?",
            (1 if paused else 0, tenant_id),
        )


def is_tenant_billing_paused(tenant_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT billing_paused FROM tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
    return bool(row and row["billing_paused"])


def set_patient_billing_paused(tenant_id: int, phone: str, paused: bool) -> None:
    """Pausa/retoma cobrança de um paciente. Garante a linha em patients
    (faz upsert leve preservando preço/nome se já existir)."""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO patients (tenant_id, phone, billing_paused)
            VALUES (?, ?, ?)
            ON CONFLICT(tenant_id, phone) DO UPDATE SET
                billing_paused = excluded.billing_paused
        """, (tenant_id, phone, 1 if paused else 0))


def is_patient_billing_paused(tenant_id: int, phone: str) -> bool:
    variants = _phone_variants(phone) or {phone}
    if phone:
        variants.add(phone)
    if not variants:
        return False
    ph = ",".join("?" * len(variants))
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT 1 FROM patients WHERE tenant_id = ? AND phone IN ({ph}) "
            f"AND billing_paused = 1 LIMIT 1",
            (tenant_id, *variants),
        ).fetchone()
    return row is not None


# ── Override de valor total de cobrança por mês ────────────────────────────────

def set_billing_override(tenant_id: int, phone: str, month: str,
                         total_amount: float, note: str = "") -> None:
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO billing_overrides (tenant_id, phone, month, total_amount, note, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(tenant_id, phone, month) DO UPDATE SET
                total_amount = excluded.total_amount,
                note = excluded.note,
                updated_at = datetime('now')
        """, (tenant_id, phone, month, float(total_amount), note))


def get_billing_override(tenant_id: int, phone: str, month: str) -> dict | None:
    """Override do mês para o paciente, tolerando variantes do telefone."""
    variants = _phone_variants(phone) or {phone}
    if phone:
        variants.add(phone)
    if not variants:
        return None
    ph = ",".join("?" * len(variants))
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM billing_overrides WHERE tenant_id = ? AND month = ? "
            f"AND phone IN ({ph}) LIMIT 1",
            (tenant_id, month, *variants),
        ).fetchone()
    return dict(row) if row else None


def delete_billing_override(tenant_id: int, phone: str, month: str) -> None:
    variants = _phone_variants(phone) or {phone}
    if phone:
        variants.add(phone)
    if not variants:
        return
    ph = ",".join("?" * len(variants))
    with get_conn() as conn:
        conn.execute(
            f"DELETE FROM billing_overrides WHERE tenant_id = ? AND month = ? "
            f"AND phone IN ({ph})",
            (tenant_id, month, *variants),
        )


def get_billing_overrides_for_month(tenant_id: int, month: str) -> list[dict]:
    """Todos os overrides de valor total do mês. Usado no disparo para cobrar
    pacientes SEM preço cadastrado que tiveram um valor definido na prévia."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT phone, total_amount, note FROM billing_overrides "
            "WHERE tenant_id = ? AND month = ?",
            (tenant_id, month),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Cobranças avulsas (manuais) ─────────────────────────────────────────────────

def add_manual_billing_entry(tenant_id: int, month: str, patient_name: str,
                             phone: str = "", sessions_count: int = 1,
                             total_amount: float = 0.0, note: str = "") -> int:
    """Cria uma cobrança avulsa para o mês. Retorna o id criado."""
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO billing_manual_entries
              (tenant_id, month, patient_name, phone, sessions_count, total_amount, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (tenant_id, month, patient_name, phone or "",
              int(sessions_count or 0), float(total_amount or 0), note or ""))
        return cur.lastrowid


def get_manual_billing_entries(tenant_id: int, month: str) -> list[dict]:
    """Cobranças avulsas do mês (mais recentes primeiro)."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM billing_manual_entries
            WHERE tenant_id = ? AND month = ?
            ORDER BY created_at DESC, id DESC
        """, (tenant_id, month)).fetchall()
    return [dict(r) for r in rows]


def get_manual_billing_entry(tenant_id: int, entry_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM billing_manual_entries WHERE tenant_id = ? AND id = ?",
            (tenant_id, entry_id),
        ).fetchone()
    return dict(row) if row else None


def update_manual_billing_entry(tenant_id: int, entry_id: int, **fields) -> None:
    """Atualiza campos de uma cobrança avulsa (patient_name, phone,
    sessions_count, total_amount, note). Ignora campos não permitidos."""
    allowed = {"patient_name", "phone", "sessions_count", "total_amount", "note"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return
    vals.extend([tenant_id, entry_id])
    with get_conn() as conn:
        conn.execute(
            f"UPDATE billing_manual_entries SET {', '.join(sets)} "
            f"WHERE tenant_id = ? AND id = ?",
            vals,
        )


def delete_manual_billing_entry(tenant_id: int, entry_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM billing_manual_entries WHERE tenant_id = ? AND id = ?",
            (tenant_id, entry_id),
        )


def mark_manual_billing_entry_sent(tenant_id: int, entry_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE billing_manual_entries SET sent_at = datetime('now') "
            "WHERE tenant_id = ? AND id = ?",
            (tenant_id, entry_id),
        )


def set_manual_billing_paid(tenant_id: int, entry_id: int, paid: bool) -> None:
    """Marca/desmarca uma cobrança avulsa como paga (via coluna paid_at).
    NÃO afeta cálculo nem disparo — só controle visual de recebimento."""
    with get_conn() as conn:
        if paid:
            conn.execute(
                "UPDATE billing_manual_entries SET paid_at = datetime('now') "
                "WHERE tenant_id = ? AND id = ? AND paid_at IS NULL",
                (tenant_id, entry_id),
            )
        else:
            conn.execute(
                "UPDATE billing_manual_entries SET paid_at = NULL "
                "WHERE tenant_id = ? AND id = ?",
                (tenant_id, entry_id),
            )


# ── Controle de pagamento (recebimento) por paciente/mês ────────────────────────
# Presença de linha em billing_payments = "pago". Ausência = "não pago".
# Puramente informativo: NÃO afeta cálculo de cobrança nem o disparo.

def set_billing_paid(tenant_id: int, phone: str, month: str, paid: bool) -> None:
    """Marca (paid=True) ou desmarca (paid=False) o pagamento do paciente no mês.
    Grava/apaga usando o telefone exato do cadastro (mesmo usado na prévia)."""
    with get_conn() as conn:
        if paid:
            conn.execute(
                "INSERT OR IGNORE INTO billing_payments (tenant_id, phone, month) "
                "VALUES (?, ?, ?)",
                (tenant_id, phone or "", month),
            )
        else:
            variants = _phone_variants(phone) or set()
            if phone:
                variants.add(phone)
            variants.add(_norm_digits(phone))
            variants = {v for v in variants if v}
            if not variants:
                conn.execute(
                    "DELETE FROM billing_payments WHERE tenant_id = ? AND month = ? AND phone = ?",
                    (tenant_id, month, phone or ""),
                )
                return
            ph = ",".join("?" * len(variants))
            conn.execute(
                f"DELETE FROM billing_payments WHERE tenant_id = ? AND month = ? "
                f"AND phone IN ({ph})",
                (tenant_id, month, *variants),
            )


def get_paid_phones_for_month(tenant_id: int, month: str) -> set[str]:
    """Retorna o conjunto de telefones (normalizados só-dígitos) marcados como
    pagos no mês. Tolerante a variantes na hora de comparar na prévia."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT phone FROM billing_payments WHERE tenant_id = ? AND month = ?",
            (tenant_id, month),
        ).fetchall()
    out: set[str] = set()
    for r in rows:
        p = r["phone"] if isinstance(r, dict) or hasattr(r, "keys") else r[0]
        for v in (_phone_variants(p) or set()):
            out.add(_norm_digits(v))
        out.add(_norm_digits(p or ""))
    return {v for v in out if v}


# ── Comprovantes recebidos (sinalização p/ confirmar pagamento) ─────────────────

def flag_billing_receipt(tenant_id: int, phone: str, month: str, kind: str = "") -> bool:
    """Registra que chegou um comprovante do paciente no mês. Reativa o sinal se
    estava dispensado. Retorna True se ficou PENDENTE agora (novo ou reativado) —
    útil pra notificar a psicóloga só na transição, sem spammar."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT dismissed_at FROM billing_receipts "
            "WHERE tenant_id = ? AND phone = ? AND month = ?",
            (tenant_id, phone or "", month),
        ).fetchone()
        was_pending = bool(row) and (row["dismissed_at"] is None)
        conn.execute("""
            INSERT INTO billing_receipts (tenant_id, phone, month, kind, received_at, dismissed_at)
            VALUES (?, ?, ?, ?, datetime('now'), NULL)
            ON CONFLICT(tenant_id, phone, month) DO UPDATE SET
                kind = excluded.kind,
                received_at = datetime('now'),
                dismissed_at = NULL
        """, (tenant_id, phone or "", month, kind or ""))
        return not was_pending


def get_pending_receipts_for_month(tenant_id: int, month: str) -> dict[str, str]:
    """Telefones (normalizados só-dígitos, tolerante a variantes) com comprovante
    pendente de confirmação no mês → mapeia para o tipo (imagem/documento/pix)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT phone, kind FROM billing_receipts "
            "WHERE tenant_id = ? AND month = ? AND dismissed_at IS NULL",
            (tenant_id, month),
        ).fetchall()
    out: dict[str, str] = {}
    for r in rows:
        p = r["phone"]
        kind = r["kind"] or ""
        keys = {_norm_digits(v) for v in (_phone_variants(p) or set())}
        keys.add(_norm_digits(p or ""))
        for k in keys:
            if k:
                out[k] = kind
    return out


def dismiss_billing_receipt(tenant_id: int, phone: str, month: str) -> None:
    """Dispensa o sinal de comprovante (falso alarme ou já resolvido), tolerando
    variantes do telefone."""
    variants = _phone_variants(phone) or set()
    if phone:
        variants.add(phone)
    variants.add(_norm_digits(phone))
    variants = {v for v in variants if v}
    if not variants:
        return
    ph = ",".join("?" * len(variants))
    with get_conn() as conn:
        conn.execute(
            f"UPDATE billing_receipts SET dismissed_at = datetime('now') "
            f"WHERE tenant_id = ? AND month = ? AND phone IN ({ph})",
            (tenant_id, month, *variants),
        )


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
    # Excluir placeholders de "novo paciente aguardando agendamento" (ano 2099)
    # para que o agente não os mostre como consultas reais ao paciente.
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM appointments
               WHERE tenant_id = ? AND phone = ? AND scheduled_at >= ?
                 AND scheduled_at < '2099-01-01'
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


def cancel_appointment(tenant_id: int, appointment_id: int) -> bool:
    """Cancelamento SUAVE: marca cancelled=1 (não apaga a linha).
    Mantém o registro para a psicóloga decidir sobre a política de cobrança
    e para as estatísticas; também faz o agendador PARAR de enviar lembretes
    (todas as queries de lembrete filtram cancelled=0)."""
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE appointments
               SET cancelled = 1, confirmation_sent = 1, followup_sent = 1
               WHERE id = ? AND tenant_id = ?""",
            (appointment_id, tenant_id),
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


def get_pending_confirmations_for_tomorrow(tenant_id: int) -> list[dict]:
    """Para o DISPARO MANUAL do painel: TODAS as consultas de AMANHÃ (dia do
    calendário, horário de Brasília) que ainda não receberam confirmação,
    independentemente da janela de 23-25h usada pelo agendamento automático.

    Diferente de get_appointments_for_confirmation (janela estrita 23-25h),
    aqui o objetivo é a psicóloga clicar e enviar para o dia inteiro de amanhã.

    Exclui: já confirmadas/enviadas, canceladas, "avisou que não vem",
    placeholders de novo paciente (2099) e pacientes com agente pausado.
    """
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("America/Sao_Paulo")
    now_br = _dt.now(_TZ).replace(tzinfo=None)
    tomorrow_str = (now_br.date() + _td(days=1)).isoformat()   # 'YYYY-MM-DD'
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM appointments
               WHERE tenant_id = ?
                 AND date(scheduled_at) = ?
                 AND confirmation_sent = 0
                 AND confirmed = 0
                 AND cancelled = 0
                 AND COALESCE(attendance, 'pending') != 'missed_with_notice'
                 AND scheduled_at < '2099-01-01'
                 AND NOT EXISTS (
                     SELECT 1 FROM agent_paused ap
                     WHERE ap.tenant_id = appointments.tenant_id
                       AND ap.phone = appointments.phone
                 )
               ORDER BY scheduled_at""",
            (tenant_id, tomorrow_str),
        ).fetchall()
    return [dict(r) for r in rows]


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
            """SELECT id FROM appointments
               WHERE tenant_id = ? AND scheduled_at = ?
                 AND cancelled = 0 AND scheduled_at < '2099-01-01'""",
            (tenant_id, dt.isoformat()),
        ).fetchone()
    return row is not None


def has_conflict(tenant_id: int, dt: datetime, duration_min: int, exclude_id: int | None = None) -> bool:
    """Detecta sobreposição real: True se `dt` cair dentro de [b - duration, b + duration]
    de qualquer consulta existente (exceto a própria, via exclude_id).

    Ignora:
    - Consultas canceladas (cancelled = 1)
    - Consultas em que o paciente avisou que não vem (missed_with_notice) —
      o horário foi liberado e pode receber outro paciente
    - Placeholders de "novo paciente" (ano >= 2099)
    """
    from datetime import datetime as _dt
    with get_conn() as conn:
        query = (
            "SELECT id, scheduled_at FROM appointments "
            "WHERE tenant_id = ? AND cancelled = 0 "
            "AND COALESCE(attendance, 'pending') != 'missed_with_notice' "
            "AND scheduled_at < '2099-01-01'"
        )
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


def set_patient_name(tenant_id: int, phone: str, name: str) -> None:
    """Grava SOMENTE o nome do paciente (preserva preço/email). Cria a linha
    se ainda não existir — assim a correção de nome persiste mesmo para
    contatos que ainda não têm agendamento."""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO patients (tenant_id, phone, name) VALUES (?, ?, ?)
            ON CONFLICT(tenant_id, phone) DO UPDATE SET name = excluded.name
        """, (tenant_id, phone, name))


def rename_patient_everywhere(tenant_id: int, phone: str, name: str) -> int:
    """Corrige o nome do paciente em TODOS os agendamentos (casando variantes
    de telefone) e persiste em patients. Retorna nº de agendamentos atualizados.
    """
    variants = _phone_variants(phone) or {phone}
    if phone:
        variants.add(phone)
    pn = _norm_digits(phone) or phone
    with get_conn() as conn:
        rows = 0
        if variants:
            ph = ",".join("?" * len(variants))
            cur = conn.execute(
                f"UPDATE appointments SET patient_name = ? "
                f"WHERE tenant_id = ? AND phone IN ({ph})",
                (name, tenant_id, *variants),
            )
            rows = cur.rowcount or 0
        # Persiste o nome no cadastro (cria se não existir) — fonte estável
        conn.execute("""
            INSERT INTO patients (tenant_id, phone, name) VALUES (?, ?, ?)
            ON CONFLICT(tenant_id, phone) DO UPDATE SET name = excluded.name
        """, (tenant_id, pn, name))
    return rows


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
    """Sessões que entram no faturamento do mês — MESMO critério da agenda.

    Critério (espelha get_session_counts_by_month, a contagem do painel):
    - ocorreram dentro do mês (e já passaram: scheduled_at <= agora)
    - cancelled = 0
    - attendance != 'missed_with_notice'  (cancelou com aviso → NÃO cobra)
    'attended', 'missed_no_notice' e 'pending' (default) entram no cálculo.

    NÃO exige confirmed=1: a sessão pode ter acontecido sem o paciente ter
    respondido "SIM" à mensagem de confirmação. A cobrança segue o calendário,
    não a resposta à confirmação (antes, sessões reais ficavam de fora).

    Casa o telefone em TODAS as variantes plausíveis (com/sem DDI 55 e o "9"
    extra do celular). O Z-API às vezes entrega o mesmo contato em formatos
    diferentes, então a sessão pode estar gravada num formato e o preço noutro.
    """
    variants = _phone_variants(phone) or {phone}
    if phone:
        variants.add(phone)
    if not variants:
        return []
    ph = ",".join("?" * len(variants))
    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT * FROM appointments
            WHERE tenant_id = ? AND phone IN ({ph})
              AND cancelled = 0
              AND COALESCE(attendance, 'pending') != 'missed_with_notice'
              AND scheduled_at >= ? AND scheduled_at < ?
              AND scheduled_at <= ?
            ORDER BY scheduled_at
        """, (tenant_id, *variants, month_start, month_end, now_str)).fetchall()
    return [dict(r) for r in rows]


def get_all_billable_appointments_for_month(tenant_id: int, month_start: str, month_end: str, now_str: str) -> list[dict]:
    """Todos os agendamentos cobráveis do mês (mesmo critério da agenda), com
    id/phone/patient_name. Usado pela PRÉVIA de cobrança para mostrar também
    contatos que têm sessão mas ainda não têm valor cadastrado."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, phone, patient_name, scheduled_at FROM appointments
            WHERE tenant_id = ?
              AND cancelled = 0
              AND COALESCE(attendance, 'pending') != 'missed_with_notice'
              AND scheduled_at >= ? AND scheduled_at < ?
              AND scheduled_at <= ?
            ORDER BY scheduled_at
        """, (tenant_id, month_start, month_end, now_str)).fetchall()
    return [dict(r) for r in rows]


def get_month_appointments_raw(tenant_id: int, month_start: str, month_end: str, now_str: str) -> list[dict]:
    """TODAS as linhas de agendamento do mês (SEM nenhum filtro de cobrança),
    para diagnóstico/auditoria. Inclui canceladas, faltas com aviso e futuras —
    cada linha traz por que entra ou não no faturamento. Read-only."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, phone, patient_name, scheduled_at, confirmed,
                   COALESCE(cancelled, 0) AS cancelled,
                   COALESCE(attendance, 'pending') AS attendance
            FROM appointments
            WHERE tenant_id = ?
              AND scheduled_at >= ? AND scheduled_at < ?
            ORDER BY scheduled_at
        """, (tenant_id, month_start, month_end)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        sa = d.get("scheduled_at") or ""
        future = sa > now_str  # ainda não ocorreu → não cobra
        billable = (
            int(d.get("cancelled") or 0) == 0
            and (d.get("attendance") or "pending") != "missed_with_notice"
            and not future
        )
        d["future"] = future
        d["counts_for_billing"] = billable
        out.append(d)
    return out


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
    """Histórico completo de um paciente (todas as consultas, ordem decrescente).
    Exclui placeholders de "novo paciente aguardando agendamento" (ano 2099).
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM appointments
               WHERE tenant_id = ? AND phone = ?
                 AND scheduled_at < '2099-01-01'
               ORDER BY scheduled_at DESC""",
            (tenant_id, phone),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Billing logs ───────────────────────────────────────────────────────────────

def billing_already_sent(tenant_id: int, phone: str, month: str) -> bool:
    # Ignora cobranças AVULSAS (channel='avulsa'): elas têm dedup próprio
    # (sent_at na entry) e não devem bloquear a cobrança regular do mês.
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM billing_logs WHERE tenant_id = ? AND phone = ? "
            "AND month = ? AND COALESCE(channel,'') != 'avulsa'",
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


def get_billing_logs_for_month(tenant_id: int, month: str) -> list[dict]:
    """Histórico de cobranças efetivamente enviadas no mês de referência
    (month no formato 'YYYY-MM'). Mais recentes primeiro."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM billing_logs WHERE tenant_id = ? AND month = ?
            ORDER BY sent_at DESC
        """, (tenant_id, month)).fetchall()
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
# Lembretes de vencimento — contas do operador (op_bills) + log idempotente
# ════════════════════════════════════════════════════════════════════════════

def op_bills_list(only_active: bool = True) -> list[dict]:
    sql = "SELECT * FROM op_bills"
    if only_active:
        sql += " WHERE active = 1"
    sql += " ORDER BY due_date ASC"
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def op_bill_add(label: str, due_date: str, amount: float = 0.0,
                recurrence: str = "monthly", notes: str = "") -> int:
    recurrence = recurrence if recurrence in ("none", "monthly", "yearly") else "monthly"
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO op_bills (label, amount, due_date, recurrence, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (label.strip()[:80], float(amount or 0), due_date[:10], recurrence, (notes or "")[:300]),
        )
        return cur.lastrowid


def op_bill_update(bill_id: int, **fields) -> bool:
    allowed = {"label", "amount", "due_date", "recurrence", "active", "notes"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [bill_id]
    with get_conn() as conn:
        cur = conn.execute(f"UPDATE op_bills SET {set_clause} WHERE id = ?", values)
        return cur.rowcount > 0


def op_bill_delete(bill_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM op_bills WHERE id = ?", (bill_id,))
        return cur.rowcount > 0


def bill_reminder_already_sent(kind: str, ref_id: int, due_date: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM bill_reminders_log WHERE kind = ? AND ref_id = ? AND due_date = ?",
            (kind, ref_id, due_date[:10]),
        ).fetchone()
    return row is not None


def mark_bill_reminder_sent(kind: str, ref_id: int, due_date: str, channel: str = ""):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO bill_reminders_log (kind, ref_id, due_date, channel)
               VALUES (?, ?, ?, ?)""",
            (kind, ref_id, due_date[:10], channel),
        )


# ── Saúde das instâncias (monitor de Z-API) ────────────────────────────────────

def instance_health_get(tenant_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM instance_health WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
    return dict(row) if row else None


def instance_health_upsert(tenant_id: int, connected, fail_count: int,
                           down_since, alerted_at, last_checked: str,
                           last_error: str = ""):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO instance_health
                   (tenant_id, connected, fail_count, down_since, alerted_at, last_checked, last_error)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tenant_id) DO UPDATE SET
                   connected   = excluded.connected,
                   fail_count  = excluded.fail_count,
                   down_since  = excluded.down_since,
                   alerted_at  = excluded.alerted_at,
                   last_checked= excluded.last_checked,
                   last_error  = excluded.last_error""",
            (tenant_id, connected, fail_count, down_since, alerted_at, last_checked, last_error),
        )


def instance_health_all() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM instance_health").fetchall()
    return [dict(r) for r in rows]


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
                   zapi_expires_at,
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
    return _decrypt_tenant(dict(row)) if row else None


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
