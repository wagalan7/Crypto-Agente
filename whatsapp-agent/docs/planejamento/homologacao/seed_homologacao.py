#!/usr/bin/env python3
"""Semeia o banco de HOMOLOGAÇÃO com dados fictícios.

Tudo aqui é inventado. Nenhum nome, telefone, e-mail ou valor corresponde a
pessoa real. Nunca aponte DATA_DIR para o volume de produção.

Uso:
    export DATA_DIR=/tmp/homolog-agente/dados
    mkdir -p "$DATA_DIR"
    python seed_homologacao.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

# Guarda-corpo: exige DATA_DIR explícito e recusa caminhos suspeitos de produção.
_dd = os.environ.get("DATA_DIR", "")
if not _dd:
    sys.exit("Defina DATA_DIR para um diretório temporário antes de rodar.")
if _dd.rstrip("/") in ("/data", "."):
    sys.exit(f"DATA_DIR={_dd!r} parece ser produção (ou o cwd). Use um diretório temporário.")
os.makedirs(_dd, exist_ok=True)

# O backend importa módulos por nome (import database), então ele entra no path.
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                        "..", "..", "..", "backend"))
sys.path.insert(0, _BACKEND)

import database as db  # noqa: E402

SLUG = "consultorio-exemplo"

# Pacientes fictícios. Telefones na faixa 5511 9xxxx-xxxx, inventados.
PACIENTES = [
    ("5511900000001", "Aurora Belmonte",  180.0, "aurora.exemplo@teste.invalid"),
    ("5511900000002", "Ivo Cardinal",     200.0, "ivo.exemplo@teste.invalid"),
    ("5511900000003", "Nadir Quintela",   180.0, ""),
    ("5511900000004", "Tulio Verano",     250.0, "tulio.exemplo@teste.invalid"),
    ("351900000005",  "Olga Brandao",     220.0, ""),   # fuso de Portugal, de propósito
]


def _next_weekday(base: datetime, weekday: int) -> datetime:
    """Próxima ocorrência de `weekday` (0=segunda) estritamente após `base`."""
    ahead = (weekday - base.weekday()) % 7 or 7
    return (base + timedelta(days=ahead)).replace(hour=15, minute=0,
                                                  second=0, microsecond=0)


def main() -> None:
    db.init_db()
    print(f"banco: {db.DB_PATH}")

    tenant = db.get_tenant(SLUG)
    if tenant:
        print(f"consultório '{SLUG}' já existe (id={tenant['id']}) — reaproveitando")
    else:
        tid = db.create_tenant(
            slug=SLUG,
            name="Consultório Exemplo (homologação)",
            psychologist_name="Dra. Vitoria Alencar",
            working_hours_start=8,
            working_hours_end=19,
            session_minutes=50,
        )
        print(f"consultório criado (id={tid})")

    db.update_tenant(
        SLUG,
        whatsapp_provider="mock",          # nunca envia mensagem de verdade
        working_days="0,1,2,3,4",          # segunda a sexta
        blocked_hours="12,13",             # almoço
        status="active",
        email="operacao.exemplo@teste.invalid",
        psychologist_phone="5511900000000",
        pix_key="chave-pix-ficticia-homologacao",
        segment="psicologia",
        dashboard_token="token-homologacao-nao-secreto",
    )
    tenant = db.get_tenant(SLUG)
    tid = tenant["id"]

    for phone, nome, preco, email in PACIENTES:
        db.upsert_patient(tid, phone, name=nome, session_price=preco, email=email)
    print(f"{len(PACIENTES)} pacientes fictícios")

    # Uma consulta futura por paciente, em dias diferentes, para exercitar
    # confirmação, remarcação e cobrança sem depender de agenda real.
    hoje = datetime.now().replace(second=0, microsecond=0)
    criadas = 0
    for i, (phone, nome, _p, _e) in enumerate(PACIENTES):
        alvo = _next_weekday(hoje, i % 5).replace(hour=9 + i)
        if db.has_conflict(tid, alvo, tenant["session_minutes"]):
            continue
        db.create_appointment(tid, nome, phone, alvo)
        criadas += 1
    print(f"{criadas} consultas futuras fictícias")

    print()
    print("slug            :", SLUG)
    print("dashboard_token :", tenant.get("dashboard_token"))
    print(f"painel          : /dashboard/{SLUG}?token={tenant.get('dashboard_token')}")
    print()
    print("Provedor = mock: nenhuma mensagem sai daqui.")


if __name__ == "__main__":
    main()
