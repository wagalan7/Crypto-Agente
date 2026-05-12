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
    con.commit()
    con.close()


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
            "SELECT id, owner, produto, created_at FROM campaigns ORDER BY created_at DESC"
        ).fetchall()
    else:
        rows = con.execute("""
            SELECT DISTINCT c.id, c.owner, c.produto, c.created_at
            FROM campaigns c
            LEFT JOIN campaign_grants g ON g.campaign_id = c.id AND g.granted_to = ?
            WHERE c.owner = ? OR g.granted_to IS NOT NULL
            ORDER BY c.created_at DESC
        """, (user, user)).fetchall()
    con.close()
    return [dict(r) for r in rows]


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


def get_campaign_grants(campaign_id: int) -> list[dict]:
    con = _con()
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT granted_to, granted_by, granted_at FROM campaign_grants WHERE campaign_id=?",
        (campaign_id,),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]
