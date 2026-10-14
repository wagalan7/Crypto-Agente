#!/usr/bin/env python3
"""Homologação ponta a ponta do release E01–E03, com provedor simulado.

Exercita o caminho inteiro contra um banco temporário e um provedor falso:

  1. recebimento (evento gravado antes do ACK)
  2. deduplicação (reentrega do provedor)
  3. agendamento correto (pedido de um dia não grava outro)
  4. resposta ao paciente (sai pelo outbox, não por envio direto)
  5. oferta numérica (o "1" reserva a opção 1 exibida)
  6. reinício do processo (retomada sem duplicar efeito)
  7. falha de envio e recuperação (incerto não reenvia; falha mantém a oferta anterior)
  8. mensagem sem id do provedor (recebe, responde, NÃO muta)

**Não envia mensagem a ninguém e não chama API paga.** O provedor é um dublê em
memória e o modelo tem respostas pré-programadas. Integrações reais (WhatsApp,
Groq/Anthropic, Stripe/MP, SMTP, Google/CalDAV) continuam NÃO VERIFICADAS: elas
dependem de contas de teste e autorização.

Uso:
    python3.11 -m venv /tmp/homolog-agente/venv
    /tmp/homolog-agente/venv/bin/pip install -r whatsapp-agent/requirements-dev.txt
    /tmp/homolog-agente/venv/bin/python \
        whatsapp-agent/docs/planejamento/homologacao/homologa_e2e.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# ── Isolamento, antes de qualquer import do backend ───────────────────────────
_dd = os.environ.get("DATA_DIR") or tempfile.mkdtemp(prefix="homologa-e2e-")
if _dd.rstrip("/") in ("/data", ".") or "/app" in _dd:
    sys.exit(f"DATA_DIR={_dd!r} parece ser produção. Use um diretório temporário.")
os.makedirs(_dd, exist_ok=True)
os.environ.update(
    DATA_DIR=_dd,
    GROQ_API_KEY="", ANTHROPIC_API_KEY="",      # nenhuma API paga
    WHATSAPP_PROVIDER="mock",
    ADMIN_PASSWORD="homologacao-local",
    MASTER_KEY="master-homologacao",
    INSTANCE_MONITOR_ENABLED="0",
    INBOX_WORKER_ENABLED="0",                   # o roteiro chama os workers
    OUTBOX_WORKER_ENABLED="0",
    INBOX_NUDGE_ON_WEBHOOK="0",
)

_BACKEND = Path(__file__).resolve().parents[3] / "backend"
sys.path.insert(0, str(_BACKEND))

import agent                     # noqa: E402
import calendar_service as cal   # noqa: E402
import database as db            # noqa: E402
import inbox                     # noqa: E402
import outbox                    # noqa: E402

SLUG = "homologacao"
PHONE = "5511900000001"
PSY = "5511933333333"

_falhas: list[str] = []
_passos = 0


def checar(ok: bool, titulo: str, detalhe: str = "") -> None:
    global _passos
    _passos += 1
    marca = "OK  " if ok else "FALHOU"
    print(f"  [{marca}] {titulo}" + (f" — {detalhe}" if detalhe else ""))
    if not ok:
        _falhas.append(titulo)


# ── Dublês ────────────────────────────────────────────────────────────────────

class ModeloSimulado:
    def __init__(self):
        self.fila: list[str] = []
        self.chamadas = 0

    def responder(self, **campos):
        import json
        self.fila.append(json.dumps({
            "intent": campos.pop("intent", "schedule"),
            "action": campos.pop("action", "none"),
            "data": campos.pop("data", {}),
            "response_text": campos.pop("response_text", "ok"),
        }, ensure_ascii=False))

    def __call__(self, system, messages, max_tokens=512, force_json=True):
        self.chamadas += 1
        if not self.fila:
            raise AssertionError("o backend chamou o modelo mais vezes que o previsto")
        return self.fila.pop(0)


class ProvedorFalso:
    """Nunca envia nada de verdade. Devolve o desfecho programado."""

    def __init__(self, desfecho="provider_accepted", erro=""):
        self.enviados: list[tuple] = []
        self.desfecho, self.erro = desfecho, erro

    def __call__(self, tenant, phone, body):
        self.enviados.append((phone, body))
        mid = f"MSG-{len(self.enviados)}"
        return self.desfecho, (mid if self.desfecho.startswith("provider") else ""), self.erro


modelo = ModeloSimulado()
agent._call_llm = modelo


def _sem_api_paga(*a, **k):
    raise AssertionError("tentativa de usar o cliente real de IA")


agent._get_groq_client = _sem_api_paga
agent._get_anthropic_client = _sem_api_paga


# ── Consultório fictício ──────────────────────────────────────────────────────

def preparar() -> dict:
    db.init_db()
    if not db.get_tenant(SLUG):
        db.create_tenant(slug=SLUG, name="Consultório Homologação",
                         psychologist_name="Dra. Vitoria Alencar",
                         working_hours_start=8, working_hours_end=19,
                         session_minutes=50)
    db.update_tenant(SLUG, whatsapp_provider="mock", working_days="0,1,2,3,4",
                     blocked_hours="12,13", status="active",
                     psychologist_phone=PSY,
                     dashboard_token="token-homologacao")
    t = db.get_tenant(SLUG)
    with db.get_conn() as conn:
        for tabela in ("appointments", "conversations", "patients", "offers",
                       "inbound_events", "outbox", "pending_effects"):
            conn.execute(f"DELETE FROM {tabela} WHERE tenant_id=?", (t["id"],))
        conn.execute("DELETE FROM applied_effects")
    db.upsert_patient(t["id"], PHONE, name="Aurora Belmonte")
    return db.get_tenant(SLUG)


def dia_util(dias: int, hora: int = 9) -> datetime:
    d = datetime.now().replace(hour=hora, minute=0, second=0, microsecond=0) \
        + timedelta(days=dias)
    while d.weekday() > 4:
        d += timedelta(days=1)
    return d


def sid(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M")


def consultas(t) -> list[datetime]:
    return [datetime.fromisoformat(a["scheduled_at"])
            for a in db.get_appointments_by_phone(t["id"], PHONE)
            if not a.get("cancelled")]


def receber(t, texto: str, msg_id: str | None) -> tuple[int, bool]:
    """Simula a chegada do webhook já autenticado e filtrado."""
    if msg_id:
        return db.inbound_record(t["id"], "evolution", msg_id, PHONE, texto)
    import uuid
    return db.inbound_record(t["id"], "evolution", f"local:{uuid.uuid4().hex}",
                             PHONE, texto, event_id_source="local")


# ── Cenários ──────────────────────────────────────────────────────────────────

def c1_recebimento_e_dedup(t):
    print("\n1+2. Recebimento durável e deduplicação")
    alvo = dia_util(9)
    rid, dup = receber(t, f"quero agendar dia {alvo:%d/%m}", "H-1")
    checar(not dup, "primeira entrega aceita", f"evento {rid}")
    e = db.inbound_get(rid)
    checar(e["state"] == "pending" and e["stage"] == "received",
           "evento gravado antes de qualquer processamento", f"{e['state']}/{e['stage']}")
    _rid2, dup2 = receber(t, f"quero agendar dia {alvo:%d/%m}", "H-1")
    checar(dup2 and _rid2 == rid, "reentrega do provedor reconhecida como duplicata")
    total = len([x for x in db.inbound_list(states=("pending",)) if x["event_id"] == "H-1"])
    checar(total == 1, "a duplicata não criou segundo evento")
    return rid, alvo


def c3_agendamento_correto(t, rid, alvo):
    print("\n3+4. Agendamento correto e resposta pelo outbox")
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                     response_text="Prontinho! Marquei para quarta, 01/01 às 07:00.")
    r = inbox.drain("homolog", limit=5)
    checar(bool(r) and r[0]["state"] == "done", "evento processado",
           str(r[0] if r else None))
    marcadas = consultas(t)
    checar(marcadas == [alvo], "gravou exatamente o dia pedido",
           f"{[f'{d:%a %d/%m %H:%M}' for d in marcadas]}")
    saidas = [o for o in db.outbox_list() if o["event_row_id"] == rid
              and o["kind"] == "reply"]
    checar(len(saidas) == 1, "resposta no outbox (nenhum envio direto)")
    canonico = cal.format_slots([alvo])[0]
    checar(canonico in saidas[0]["body"] and "01/01" not in saidas[0]["body"],
           "confirmação canônica, sem o texto errado do modelo")
    prov = ProvedorFalso()
    outbox.flush("homolog", limit=5, sender=prov)
    checar(len(prov.enviados) == 1, "envio feito uma única vez")
    checar(db.outbox_get(saidas[0]["id"])["state"] == "provider_accepted",
           "aceite do provedor registrado")


def c5_oferta_numerica(t):
    print("\n5. Oferta numérica")
    alvo = dia_util(16)
    opcoes = [alvo.replace(hour=h) for h in (9, 10, 11)]
    rid, _ = receber(t, f"quero agendar dia {alvo:%d/%m}", "H-2")
    modelo.responder(action="list_slots",
                     data={"offered_ids": [sid(s) for s in opcoes]},
                     response_text="texto do modelo, descartado")
    inbox.drain("homolog", limit=5)
    ev = db.inbound_get(rid)
    oferta = db.offer_get(ev["offer_id"])
    checar(oferta["state"] == "draft" and not oferta["eligible"],
           "oferta não vale antes de a mensagem sair")
    corpo = [o for o in db.outbox_list() if o["offer_id"] == ev["offer_id"]][0]["body"]
    exibidos = [l.strip() for l in corpo.splitlines() if l.strip()[:2] in
                ("1.", "2.", "3.")]
    checar(exibidos == [f"{i}. {s}" for i, s in
                        enumerate(cal.format_slots(oferta["slots"]), 1)],
           "texto exibido é exatamente a oferta persistida")
    outbox.flush("homolog", limit=5, sender=ProvedorFalso())
    checar(db.offer_get(ev["offer_id"])["eligible"],
           "oferta passa a valer após o aceite do provedor")

    receber(t, "quero o 1", "H-3")
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_index": 1},
                     response_text="Marquei!")
    inbox.drain("homolog", limit=5)
    checar(consultas(t) == [opcoes[0]], "o '1' reservou a opção 1 exibida",
           f"{[f'{d:%H:%M}' for d in consultas(t)]}")
    outbox.flush("homolog", limit=5, sender=ProvedorFalso())
    return opcoes


def c6_reinicio(t, opcoes):
    print("\n6. Reinício do processo (retomada)")
    antes = consultas(t)
    rid, _ = receber(t, f"remarcar dia {dia_util(23):%d/%m}", "H-4")
    novo = dia_util(23)
    modelo.responder(intent="reschedule", action="update",
                     data={"appointment_id": db.get_appointments_by_phone(
                         t["id"], PHONE)[0]["id"], "slot_id": sid(novo)},
                     response_text="Remarquei!")
    inbox.drain("homolog", limit=5)
    checar(consultas(t) == [novo], "remarcação aplicada", f"{novo:%d/%m %H:%M}")
    saidas_antes = len([o for o in db.outbox_list() if o["event_row_id"] == rid])

    # Simula queda e reinício: o evento volta à fila e é reprocessado.
    db.inbound_force_pending(rid)
    with db.get_conn() as conn:
        conn.execute("UPDATE inbound_events SET next_attempt_at=NULL, decision=NULL "
                     "WHERE id=?", (rid,))
    modelo.responder(intent="reschedule", action="update",
                     data={"appointment_id": db.get_appointments_by_phone(
                         t["id"], PHONE)[0]["id"], "slot_id": sid(novo)},
                     response_text="Remarquei!")
    inbox.drain("homolog", limit=5)
    checar(consultas(t) == [novo], "retomada não moveu a consulta de novo")
    saidas_depois = len([o for o in db.outbox_list() if o["event_row_id"] == rid])
    checar(saidas_depois == saidas_antes,
           "retomada não duplicou mensagem no outbox",
           f"{saidas_antes} → {saidas_depois}")
    outbox.flush("homolog", limit=10, sender=ProvedorFalso())


def c7_falha_de_envio(t):
    print("\n7. Falha de envio e recuperação")
    anterior = [dia_util(30, 9)]
    oid_a = db.offer_create(t["id"], PHONE, anterior)["offer_id"]
    db.offer_mark_sent(oid_a, "provider_accepted")
    nova = [dia_util(37, 9)]
    oid_b = db.offer_create(t["id"], PHONE, nova)["offer_id"]
    saida = db.outbox_enqueue(t["id"], PHONE, "nova lista", "homolog-b",
                              offer_id=oid_b)
    db.offer_set_outbox(oid_b, saida)
    checar(db.offer_context(t["id"], PHONE)["ativa"]["offer_id"] == oid_a,
           "oferta visível não é trocada por uma não enviada")

    # Envio incerto: não reenvia e torna a escolha ambígua.
    outbox.flush("homolog", limit=5, sender=ProvedorFalso("uncertain", "timeout"))
    checar(db.outbox_get(saida)["state"] == "uncertain", "envio marcado como incerto")
    prov = ProvedorFalso()
    outbox.flush("homolog", limit=5, sender=prov)
    checar(prov.enviados == [], "envio incerto NÃO é reenviado às cegas")
    checar(db.offer_context(t["id"], PHONE)["ambigua"],
           "escolha numérica fica ambígua entre as versões")

    # Falha definitiva: a oferta nova é cancelada e a anterior volta a valer.
    db.outbox_resolve(saida, "failed", "homologação: paciente não recebeu")
    db.offer_mark_sent(oid_b, "failed")
    ctx = db.offer_context(t["id"], PHONE)
    checar(db.offer_get(oid_b)["state"] == "cancelled", "oferta nova cancelada")
    checar(ctx["ativa"] and ctx["ativa"]["offer_id"] == oid_a and not ctx["ambigua"],
           "a oferta anterior volta a ser a válida")


def c8_sem_id_do_provedor(t):
    print("\n8. Mensagem sem id do provedor")
    antes = consultas(t)
    alvo = dia_util(44)
    receber(t, f"quero agendar dia {alvo:%d/%m}", None)
    modelo.responder(action="create",
                     data={"patient_name": "Aurora Belmonte", "slot_id": sid(alvo)},
                     response_text="Marquei!")
    inbox.drain("homolog", limit=5)
    checar(consultas(t) == antes, "nenhuma mutação automática sem id confiável")
    pend = [p for p in db.pendencia_list() if p["kind"].startswith("bloqueado:")]
    checar(bool(pend), "pendência registrada para revisão",
           pend[0]["kind"] if pend else "")
    respostas = [o for o in db.outbox_list(states=("pending",)) if o["kind"] == "reply"]
    checar(bool(respostas), "o paciente ainda recebeu resposta")


def main() -> int:
    print("=" * 72)
    print("HOMOLOGAÇÃO PONTA A PONTA — release E01–E03")
    print(f"banco temporário: {db.DB_PATH}")
    print("provedor: dublê em memória · modelo: respostas programadas")
    print("nenhuma mensagem real, nenhuma API paga")
    print("=" * 72)
    t = preparar()
    rid, alvo = c1_recebimento_e_dedup(t)
    c3_agendamento_correto(t, rid, alvo)
    opcoes = c5_oferta_numerica(t)
    c6_reinicio(t, opcoes)
    c7_falha_de_envio(t)
    c8_sem_id_do_provedor(t)

    print("\n" + "=" * 72)
    print(f"{_passos - len(_falhas)}/{_passos} verificações passaram")
    if _falhas:
        print("FALHAS:")
        for f in _falhas:
            print(f"  - {f}")
    print("\nNÃO VERIFICADO (depende de conta de teste e autorização):")
    for item in ("WhatsApp real (Evolution/Z-API)", "modelo real (Groq/Anthropic)",
                 "Stripe / Mercado Pago", "SMTP", "Google Calendar / CalDAV",
                 "backup e restauração em S3/R2/B2"):
        print(f"  - {item}")
    print("=" * 72)
    return 1 if _falhas else 0


if __name__ == "__main__":
    sys.exit(main())
