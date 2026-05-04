"""
Seed multi-tenant: cria 2 consultórios com pacientes de exemplo.
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime, timedelta
import database as db
import tenant_service as ts


def seed():
    db.init_db()

    with db.get_conn() as conn:
        conn.execute("DELETE FROM appointments")
        conn.execute("DELETE FROM conversations")
        conn.execute("DELETE FROM tenants")

    # ── Consultório 1 ──────────────────────────────────────────────────────────
    t1 = ts.create_tenant(
        name="Consultório Dra. Ana Lima",
        psychologist_name="Dra. Ana Lima",
        working_hours_start=8,
        working_hours_end=18,
        session_minutes=50,
        slug="dra-ana",
    )
    print(f"✓ Tenant: {t1['name']} (slug={t1['slug']})")

    # ── Consultório 2 ──────────────────────────────────────────────────────────
    t2 = ts.create_tenant(
        name="Psicologia Bem Estar",
        psychologist_name="Dr. Carlos Melo",
        working_hours_start=7,
        working_hours_end=21,
        session_minutes=60,
        slug="bem-estar",
    )
    print(f"✓ Tenant: {t2['name']} (slug={t2['slug']})")

    now = datetime.now().replace(minute=0, second=0, microsecond=0)

    appointments_t1 = [
        ("Maria Silva",   "11911110001", now + timedelta(hours=25), True),
        ("João Pereira",  "11922220002", now + timedelta(hours=26), False),
        ("Ana Costa",     "11933330003", now + timedelta(days=2, hours=2), False),
    ]
    for name, phone, dt, confirmed in appointments_t1:
        aid = db.create_appointment(t1["id"], name, phone, dt)
        if confirmed:
            db.confirm_appointment(t1["id"], aid)
        print(f"  → [{t1['slug']}] {name} — {dt.strftime('%d/%m %H:%M')} {'✓' if confirmed else ''}")

    appointments_t2 = [
        ("Carlos Mendes", "11944440004", now + timedelta(hours=24), False),
        ("Fernanda Lima",  "11955550005", now + timedelta(days=3, hours=1), True),
    ]
    for name, phone, dt, confirmed in appointments_t2:
        aid = db.create_appointment(t2["id"], name, phone, dt)
        if confirmed:
            db.confirm_appointment(t2["id"], aid)
        print(f"  → [{t2['slug']}] {name} — {dt.strftime('%d/%m %H:%M')} {'✓' if confirmed else ''}")

    print(f"\nSeed concluído: 2 consultórios, {len(appointments_t1)+len(appointments_t2)} consultas.")
    print(f"\nWebhooks:")
    print(f"  POST /webhook/dra-ana/evolution")
    print(f"  POST /webhook/bem-estar/evolution")
    print(f"\nTeste local:")
    print(f"  POST /test/dra-ana/message")
    print(f"  POST /test/bem-estar/message")


if __name__ == "__main__":
    seed()
