"""
Script de uso único: conceder acesso gratuito por 5 anos ao consultório da Bruna.
Execute via: railway run python3 set_free_access.py
"""
import sys
import sqlite3
import os

sys.path.insert(0, os.path.dirname(__file__))
import config
import database as db

FREE_UNTIL = "2031-05-07"  # 5 anos a partir de hoje (2026-05-07)

# Inicializa DB (aplica migrações, incluindo a nova coluna free_until)
db.init_db()

with db.get_conn() as conn:
    rows = conn.execute("SELECT id, slug, name, status, free_until FROM tenants WHERE active = 1").fetchall()

print("Tenants cadastrados:")
for r in rows:
    print(f"  id={r['id']}  slug={r['slug']}  name={r['name']}  status={r['status']}  free_until={r['free_until']}")

if not rows:
    print("Nenhum tenant encontrado.")
    sys.exit(1)

# Se houver só um, usa ele; caso contrário, pede confirmação
if len(rows) == 1:
    target = dict(rows[0])
else:
    print("\nQual slug deve receber acesso gratuito? (ex: bruna-parolin)")
    slug_input = input("> ").strip()
    target = next((dict(r) for r in rows if r["slug"] == slug_input), None)
    if not target:
        print(f"Slug '{slug_input}' não encontrado.")
        sys.exit(1)

print(f"\nConcedendo acesso gratuito até {FREE_UNTIL} para: {target['name']} ({target['slug']})")

with db.get_conn() as conn:
    conn.execute(
        "UPDATE tenants SET free_until = ?, status = 'active' WHERE slug = ?",
        (FREE_UNTIL, target["slug"])
    )

# Verificação
with db.get_conn() as conn:
    row = conn.execute("SELECT slug, name, status, free_until FROM tenants WHERE slug = ?",
                       (target["slug"],)).fetchone()

print(f"\n✅ Atualizado: slug={row['slug']} | status={row['status']} | free_until={row['free_until']}")
print("Acesso gratuito configurado com sucesso por 5 anos!")
