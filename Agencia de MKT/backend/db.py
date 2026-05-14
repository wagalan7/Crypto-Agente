"""
Campaign history database — SQLite stored at /data/mkt.db (Railway Volume)
Falls back to /tmp if /data is not mounted.
"""
import sqlite3
import json
import os
from pathlib import Path

# Use /data if a Railway Volume is mounted there, otherwise /tmp
_data_dir = Path("/data") if Path("/data").exists() and os.access("/data", os.W_OK) else Path("/tmp")
DB_PATH = _data_dir / "mkt.db"


def _con():
    con = sqlite3.connect(str(DB_PATH))
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    con = _con()
    con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username   TEXT PRIMARY KEY,
            password   TEXT NOT NULL,
            role       TEXT NOT NULL DEFAULT 'user',
            name       TEXT NOT NULL DEFAULT ''
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS credentials (
            owner      TEXT NOT NULL,
            platform   TEXT NOT NULL,
            cred_key   TEXT NOT NULL,
            cred_value TEXT NOT NULL,
            PRIMARY KEY (owner, platform, cred_key)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS campaigns (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            owner       TEXT NOT NULL,
            produto     TEXT,
            input_json  TEXT,
            result_json TEXT,
            created_at  TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS campaign_grants (
            campaign_id INTEGER NOT NULL,
            granted_to  TEXT NOT NULL,
            granted_by  TEXT NOT NULL,
            granted_at  TEXT DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (campaign_id, granted_to)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS alert_rules (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            owner      TEXT NOT NULL,
            platform   TEXT NOT NULL DEFAULT 'google',
            metric     TEXT NOT NULL,
            condition  TEXT NOT NULL,
            threshold  REAL NOT NULL,
            label      TEXT,
            active     INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            owner      TEXT NOT NULL,
            message    TEXT NOT NULL,
            level      TEXT NOT NULL DEFAULT 'warning',
            read       INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_posts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            owner        TEXT NOT NULL,
            text         TEXT NOT NULL,
            image_url    TEXT,
            platforms    TEXT NOT NULL,
            creds_json   TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            result_json  TEXT,
            created_at   TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            name   TEXT PRIMARY KEY,
            run_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS client_profiles (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            owner        TEXT NOT NULL,
            client_name  TEXT NOT NULL,
            credentials  TEXT NOT NULL DEFAULT '{}',
            created_at   TEXT DEFAULT (datetime('now','localtime')),
            updated_at   TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    con.commit()

    # One-time migrations
    _migrate_cleanup_unpublished(con)

    con.close()


def _migrate_cleanup_unpublished(con):
    """
    One-time migration: delete every campaign that was auto-saved by the old
    pipeline-done trigger but was never actually published to any platform.
    Campaigns saved after the fix already have _published_platforms in result_json.
    """
    done = con.execute(
        "SELECT 1 FROM _migrations WHERE name='cleanup_unpublished_v1'"
    ).fetchone()
    if done:
        return

    rows = con.execute("SELECT id, result_json FROM campaigns").fetchall()
    to_delete = []
    for cid, rj in rows:
        try:
            d = json.loads(rj or "{}")
            raw = d.get("_published_platforms", "[]")
            platforms = json.loads(raw) if isinstance(raw, str) else raw
            if not platforms:          # empty list = never published
                to_delete.append(cid)
        except Exception:
            to_delete.append(cid)      # unparseable = treat as unpublished

    if to_delete:
        ph = ",".join("?" for _ in to_delete)
        con.execute(f"DELETE FROM campaigns WHERE id IN ({ph})", to_delete)
        con.execute(f"DELETE FROM campaign_grants WHERE campaign_id IN ({ph})", to_delete)

    con.execute("INSERT INTO _migrations (name) VALUES ('cleanup_unpublished_v1')")
    con.commit()


# ── Users ─────────────────────────────────────────────────────────────────────

def db_list_users() -> list[dict]:
    con = _con()
    rows = con.execute("SELECT username, role, name FROM users").fetchall()
    con.close()
    return [{"user": r[0], "role": r[1], "name": r[2]} for r in rows]


def db_get_user(username: str) -> dict | None:
    con = _con()
    row = con.execute(
        "SELECT username, password, role, name FROM users WHERE username=?", (username,)
    ).fetchone()
    con.close()
    if not row:
        return None
    return {"user": row[0], "pass": row[1], "role": row[2], "name": row[3]}


def db_add_user(username: str, password: str, role: str = "user", name: str = "") -> dict:
    con = _con()
    con.execute(
        "INSERT INTO users (username, password, role, name) VALUES (?,?,?,?)",
        (username, password, role, name)
    )
    con.commit()
    con.close()
    return {"user": username, "role": role, "name": name}


def db_update_user(username: str, new_username: str | None = None,
                   new_password: str | None = None, new_name: str | None = None):
    con = _con()
    if new_password:
        con.execute("UPDATE users SET password=? WHERE username=?", (new_password, username))
    if new_name is not None:
        con.execute("UPDATE users SET name=? WHERE username=?", (new_name, username))
    if new_username and new_username != username:
        con.execute("UPDATE users SET username=? WHERE username=?", (new_username, username))
    con.commit()
    con.close()


def db_delete_user(username: str):
    con = _con()
    con.execute("DELETE FROM users WHERE username=?", (username,))
    con.commit()
    con.close()


def db_seed_users(users: list[dict]):
    """Seed users from env/defaults if the table is empty."""
    con = _con()
    count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count == 0:
        for u in users:
            con.execute(
                "INSERT OR IGNORE INTO users (username, password, role, name) VALUES (?,?,?,?)",
                (u.get("user", ""), u.get("pass", ""), u.get("role", "user"), u.get("name", ""))
            )
        con.commit()
    con.close()


# ── Credentials ───────────────────────────────────────────────────────────────

def save_credential(owner: str, platform: str, key: str, value: str):
    con = _con()
    con.execute("""
        INSERT INTO credentials (owner, platform, cred_key, cred_value) VALUES (?,?,?,?)
        ON CONFLICT(owner, platform, cred_key) DO UPDATE SET cred_value=excluded.cred_value
    """, (owner, platform, key, value))
    con.commit()
    con.close()


def get_credentials(owner: str) -> dict:
    con = _con()
    rows = con.execute(
        "SELECT platform, cred_key, cred_value FROM credentials WHERE owner=?", (owner,)
    ).fetchall()
    con.close()
    result: dict = {}
    for platform, key, value in rows:
        result.setdefault(platform, {})[key] = value
    return result


def delete_platform_credentials(owner: str, platform: str):
    con = _con()
    con.execute("DELETE FROM credentials WHERE owner=? AND platform=?", (owner, platform))
    con.commit()
    con.close()


# ── Campaigns ─────────────────────────────────────────────────────────────────

def save_campaign(owner: str, produto: str, input_data: dict, result_data: dict) -> int:
    con = _con()
    cur = con.execute(
        "INSERT INTO campaigns (owner, produto, input_json, result_json) VALUES (?,?,?,?)",
        (owner, produto,
         json.dumps(input_data, ensure_ascii=False),
         json.dumps(result_data, ensure_ascii=False)),
    )
    cid = cur.lastrowid
    con.commit()
    con.close()
    return cid


def list_campaigns(user: str, is_admin: bool) -> list[dict]:
    con = _con()
    con.row_factory = sqlite3.Row
    if is_admin:
        rows = con.execute(
            "SELECT id, owner, produto, created_at, result_json FROM campaigns ORDER BY created_at DESC"
        ).fetchall()
    else:
        rows = con.execute("""
            SELECT DISTINCT c.id, c.owner, c.produto, c.created_at, c.result_json
            FROM campaigns c
            LEFT JOIN campaign_grants g ON g.campaign_id = c.id AND g.granted_to = ?
            WHERE c.owner = ? OR g.granted_to IS NOT NULL
            ORDER BY c.created_at DESC
        """, (user, user)).fetchall()
    con.close()
    result = []
    for r in rows:
        d = dict(r)
        # Parse published_platforms from result_json without loading full content
        try:
            rj = json.loads(d.get("result_json") or "{}")
            raw = rj.get("_published_platforms", "[]")
            d["published_platforms"] = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            d["published_platforms"] = []
        del d["result_json"]
        result.append(d)
    return result


def get_campaign(campaign_id: int, user: str, is_admin: bool) -> dict | None:
    con = _con()
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if not row:
        con.close()
        return None
    d = dict(row)
    if not is_admin and d["owner"] != user:
        grant = con.execute(
            "SELECT 1 FROM campaign_grants WHERE campaign_id=? AND granted_to=?",
            (campaign_id, user),
        ).fetchone()
        if not grant:
            con.close()
            return None
    con.close()
    d["input_data"]  = json.loads(d["input_json"]  or "{}")
    d["result_data"] = json.loads(d["result_json"] or "{}")
    return d


def grant_access(campaign_id: int, granted_to: str, granted_by: str):
    con = _con()
    con.execute(
        "INSERT OR REPLACE INTO campaign_grants (campaign_id, granted_to, granted_by) VALUES (?,?,?)",
        (campaign_id, granted_to, granted_by),
    )
    con.commit()
    con.close()


def revoke_access(campaign_id: int, granted_to: str):
    con = _con()
    con.execute(
        "DELETE FROM campaign_grants WHERE campaign_id=? AND granted_to=?",
        (campaign_id, granted_to),
    )
    con.commit()
    con.close()


# ── Alert Rules ──────────────────────────────────────────────────────────────

def list_alert_rules(owner: str) -> list[dict]:
    con = _con(); con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM alert_rules WHERE owner=? ORDER BY created_at DESC", (owner,)).fetchall()
    con.close()
    return [dict(r) for r in rows]

def create_alert_rule(owner: str, platform: str, metric: str, condition: str,
                      threshold: float, label: str = "") -> int:
    con = _con()
    cur = con.execute(
        "INSERT INTO alert_rules (owner, platform, metric, condition, threshold, label) VALUES (?,?,?,?,?,?)",
        (owner, platform, metric, condition, threshold, label),
    )
    rid = cur.lastrowid; con.commit(); con.close()
    return rid

def delete_alert_rule(rule_id: int, owner: str):
    con = _con()
    con.execute("DELETE FROM alert_rules WHERE id=? AND owner=?", (rule_id, owner))
    con.commit(); con.close()

def get_all_active_alert_rules() -> list[dict]:
    con = _con(); con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM alert_rules WHERE active=1").fetchall()
    con.close()
    return [dict(r) for r in rows]

# ── Notifications ─────────────────────────────────────────────────────────────

def create_notification(owner: str, message: str, level: str = "warning") -> int:
    con = _con()
    cur = con.execute(
        "INSERT INTO notifications (owner, message, level) VALUES (?,?,?)",
        (owner, message, level),
    )
    nid = cur.lastrowid; con.commit(); con.close()
    return nid

def list_notifications(owner: str, unread_only: bool = False) -> list[dict]:
    con = _con(); con.row_factory = sqlite3.Row
    q = "SELECT * FROM notifications WHERE owner=?"
    if unread_only: q += " AND read=0"
    q += " ORDER BY created_at DESC LIMIT 50"
    rows = con.execute(q, (owner,)).fetchall()
    con.close()
    return [dict(r) for r in rows]

def mark_notifications_read(owner: str):
    con = _con()
    con.execute("UPDATE notifications SET read=1 WHERE owner=?", (owner,))
    con.commit(); con.close()

def count_unread(owner: str) -> int:
    con = _con()
    n = con.execute("SELECT COUNT(*) FROM notifications WHERE owner=? AND read=0", (owner,)).fetchone()[0]
    con.close()
    return n

# ── Client Stats (admin) ──────────────────────────────────────────────────────

def get_client_stats(username: str | None = None) -> list[dict]:
    """Returns campaign count and last activity per user.
    If username is provided, returns stats for that user only."""
    con = _con(); con.row_factory = sqlite3.Row
    if username:
        rows = con.execute("""
            SELECT u.username, u.name, u.role,
                   COUNT(c.id) as campaign_count,
                   MAX(c.created_at) as last_activity
            FROM users u
            LEFT JOIN campaigns c ON c.owner = u.username
            WHERE u.username = ?
            GROUP BY u.username
        """, (username,)).fetchall()
    else:
        rows = con.execute("""
            SELECT u.username, u.name, u.role,
                   COUNT(c.id) as campaign_count,
                   MAX(c.created_at) as last_activity
            FROM users u
            LEFT JOIN campaigns c ON c.owner = u.username
            GROUP BY u.username
            ORDER BY last_activity DESC NULLS LAST
        """).fetchall()
    con.close()
    return [dict(r) for r in rows]


# ── Scheduled Posts ───────────────────────────────────────────────────────────

def create_scheduled_post(owner: str, text: str, image_url: str,
                          platforms: list, creds: dict, scheduled_at: str) -> int:
    con = _con()
    cur = con.execute(
        "INSERT INTO scheduled_posts (owner, text, image_url, platforms, creds_json, scheduled_at) VALUES (?,?,?,?,?,?)",
        (owner, text, image_url or "", json.dumps(platforms), json.dumps(creds), scheduled_at),
    )
    pid = cur.lastrowid
    con.commit(); con.close()
    return pid


def list_scheduled_posts(owner: str, is_admin: bool) -> list[dict]:
    con = _con(); con.row_factory = sqlite3.Row
    if is_admin:
        rows = con.execute(
            "SELECT id,owner,text,platforms,scheduled_at,status,result_json,created_at FROM scheduled_posts ORDER BY scheduled_at DESC"
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT id,owner,text,platforms,scheduled_at,status,result_json,created_at FROM scheduled_posts WHERE owner=? ORDER BY scheduled_at DESC",
            (owner,),
        ).fetchall()
    con.close()
    result = []
    for r in rows:
        d = dict(r)
        d["platforms"] = json.loads(d["platforms"] or "[]")
        d["result"] = json.loads(d["result_json"] or "null")
        del d["result_json"]
        result.append(d)
    return result


def get_pending_posts(now_iso: str) -> list[dict]:
    """Return pending posts whose scheduled_at <= now."""
    con = _con(); con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM scheduled_posts WHERE status='pending' AND scheduled_at <= ?", (now_iso,)
    ).fetchall()
    con.close()
    result = []
    for r in rows:
        d = dict(r)
        d["platforms"] = json.loads(d["platforms"] or "[]")
        d["creds"]     = json.loads(d["creds_json"] or "{}")
        result.append(d)
    return result


def update_post_status(post_id: int, status: str, result: dict | None = None):
    con = _con()
    con.execute(
        "UPDATE scheduled_posts SET status=?, result_json=? WHERE id=?",
        (status, json.dumps(result) if result else None, post_id),
    )
    con.commit(); con.close()


def cancel_scheduled_post(post_id: int, owner: str, is_admin: bool) -> bool:
    con = _con()
    if is_admin:
        cur = con.execute("UPDATE scheduled_posts SET status='cancelled' WHERE id=? AND status='pending'", (post_id,))
    else:
        cur = con.execute("UPDATE scheduled_posts SET status='cancelled' WHERE id=? AND owner=? AND status='pending'", (post_id, owner))
    changed = cur.rowcount > 0
    con.commit(); con.close()
    return changed


def get_campaign_grants(campaign_id: int) -> list[dict]:
    con = _con()
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT granted_to, granted_by, granted_at FROM campaign_grants WHERE campaign_id=?",
        (campaign_id,),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


# ── Client Profiles ───────────────────────────────────────────────────────────

def list_client_profiles(owner: str, is_admin: bool) -> list[dict]:
    con = _con(); con.row_factory = sqlite3.Row
    if is_admin:
        rows = con.execute(
            "SELECT * FROM client_profiles ORDER BY client_name"
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM client_profiles WHERE owner=? ORDER BY client_name",
            (owner,)
        ).fetchall()
    con.close()
    result = []
    for r in rows:
        d = dict(r)
        d['credentials'] = json.loads(d.get('credentials') or '{}')
        result.append(d)
    return result


def create_client_profile(owner: str, client_name: str, credentials: dict) -> dict:
    con = _con()
    cur = con.execute(
        "INSERT INTO client_profiles (owner, client_name, credentials) VALUES (?,?,?)",
        (owner, client_name, json.dumps(credentials))
    )
    pid = cur.lastrowid
    con.commit(); con.close()
    return {"id": pid, "owner": owner, "client_name": client_name, "credentials": credentials}


def update_client_profile(profile_id: int, owner: str, is_admin: bool,
                          client_name: str | None = None,
                          credentials: dict | None = None) -> bool:
    con = _con()
    profile = con.execute("SELECT * FROM client_profiles WHERE id=?", (profile_id,)).fetchone()
    if not profile:
        con.close(); return False
    if not is_admin and profile[1] != owner:   # owner column
        con.close(); return False
    name  = client_name  if client_name  is not None else profile[2]
    creds = json.dumps(credentials) if credentials is not None else profile[3]
    con.execute(
        "UPDATE client_profiles SET client_name=?, credentials=?, updated_at=datetime('now','localtime') WHERE id=?",
        (name, creds, profile_id)
    )
    con.commit(); con.close()
    return True


def delete_client_profile(profile_id: int, owner: str, is_admin: bool) -> bool:
    con = _con()
    if is_admin:
        cur = con.execute("DELETE FROM client_profiles WHERE id=?", (profile_id,))
    else:
        cur = con.execute("DELETE FROM client_profiles WHERE id=? AND owner=?", (profile_id, owner))
    changed = cur.rowcount > 0
    con.commit(); con.close()
    return changed
