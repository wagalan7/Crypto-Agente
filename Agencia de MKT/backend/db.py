"""
Campaign history database — SQLite stored at /tmp/mkt_campaigns.db
"""
import sqlite3
import json
from pathlib import Path

DB_PATH = Path("/tmp/mkt_campaigns.db")


def init_db():
    con = sqlite3.connect(str(DB_PATH))
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


def save_campaign(owner: str, produto: str, input_data: dict, result_data: dict) -> int:
    con = sqlite3.connect(str(DB_PATH))
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
    con = sqlite3.connect(str(DB_PATH))
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
    con = sqlite3.connect(str(DB_PATH))
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
    con = sqlite3.connect(str(DB_PATH))
    con.execute(
        "INSERT OR REPLACE INTO campaign_grants (campaign_id, granted_to, granted_by) VALUES (?,?,?)",
        (campaign_id, granted_to, granted_by),
    )
    con.commit()
    con.close()


def revoke_access(campaign_id: int, granted_to: str):
    con = sqlite3.connect(str(DB_PATH))
    con.execute(
        "DELETE FROM campaign_grants WHERE campaign_id=? AND granted_to=?",
        (campaign_id, granted_to),
    )
    con.commit()
    con.close()


def get_campaign_grants(campaign_id: int) -> list[dict]:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT granted_to, granted_by, granted_at FROM campaign_grants WHERE campaign_id=?",
        (campaign_id,),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]
