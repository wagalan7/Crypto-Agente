"""Processamento durável das mensagens recebidas (E03).

O problema que resolve: até a build 67 o webhook respondia `200 queued` e
processava a mensagem num `BackgroundTask`. Se o processo caísse no meio — ou se
o deploy reiniciasse o container naquele instante — a mensagem do paciente
**desaparecia**: o provedor considerava entregue e nada reprocessava.

Desenho, em três partes:

1. **Receber e gravar.** O webhook autentica, extrai e grava o evento em
   `inbound_events` ANTES de responder 2xx (ver `main.webhook_*`). Se a gravação
   falhar, não há 2xx de sucesso — o provedor pode reentregar.
2. **Processar com posse.** Este módulo toma posse de um evento por `lease` com
   `attempt_token`, avança por estágios e grava cada avanço. Um worker que
   perdeu o lease não consegue mais alterar o evento.
3. **Responder pelo outbox.** A resposta não é enviada aqui: é enfileirada em
   `outbox`. Quem envia é `outbox.py`, que distingue aceite do provedor de
   entrega comprovada.

Estágios de um evento:

    received → decided → mutated → enqueued → finished
                                               (ou skipped, quando não há o que
                                                responder: agente pausado, spam,
                                                consultório suspenso)

O que este módulo **não** faz: não garante entrega exactly-once a serviço
externo, não habilita mais de uma réplica do processo web e não reconstitui a
ordem original das mensagens no WhatsApp — a ordem aqui é a de **chegada
persistida**, que é o que se pode afirmar.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import database as db

logger = logging.getLogger(__name__)
_TZ = ZoneInfo("America/Sao_Paulo")

# Quanto tempo a posse vale. Tem de ser maior que o pior caso de uma chamada ao
# LLM; menor que isso faria dois workers processarem o mesmo evento.
LEASE_SEC = getattr(config, "INBOX_LEASE_SEC", 180)
MAX_ATTEMPTS = getattr(config, "INBOX_MAX_ATTEMPTS", 5)
# Backoff: 30s, 2min, 8min, 32min… limitado para o suporte poder agir antes.
_BACKOFF = (30, 120, 480, 1920, 3600)


def _agora() -> datetime:
    return datetime.now(_TZ).replace(tzinfo=None)


def _backoff(tentativas: int) -> int:
    return _BACKOFF[min(max(tentativas, 1) - 1, len(_BACKOFF) - 1)]


def nome_worker() -> str:
    return f"{socket.gethostname()}:{threading.get_ident()}"


# ── Pipeline de um evento ─────────────────────────────────────────────────────

def _carregar_tenant(evento: dict) -> dict | None:
    t = db.get_tenant_by_id(evento["tenant_id"])
    if not t:
        logger.error(f"[inbox] evento {evento['id']} sem consultório "
                     f"{evento['tenant_id']}")
    return t


# Marcadores de mídia que o extractor do Z-API produz. O caminho durável não os
# trata: as respostas de mídia são tratadas pelo fluxo legado em
# `main._handle_message`, com envio direto. Limitação registrada; tratar mídia
# de verdade (inclusive na Evolution, onde hoje ela é descartada) é **E05**.
_MARCAS_MIDIA = ("__IMAGEM__", "__DOCUMENTO__", "__COMPROVANTE_PIX__")


def _e_midia(body: str) -> bool:
    b = (body or "").strip()
    return b in _MARCAS_MIDIA or b.startswith("__IMAGEM__:")


def _deve_ignorar(tenant: dict, phone: str, body: str = "") -> str:
    """Motivo para encerrar o evento sem passar pelo agente, ou string vazia."""
    status = tenant.get("status", "active")
    if status == "suspended" and not db.is_tenant_exempt(tenant):
        return "consultorio_suspenso"
    if db.is_agent_paused(tenant["id"], phone):
        return "agente_pausado"
    if not phone or len(phone) > 15 or phone.startswith("120363"):
        return "remetente_invalido"
    try:
        import main as _m
        if _m._is_spam(body):
            # Propaganda. A resposta educada de primeiro contato é do fluxo
            # legado; aqui o evento é encerrado sem acionar o agente.
            return "spam"
    except Exception:
        pass
    return ""


def _decidir(tenant: dict, evento: dict, agent_mod) -> dict:
    """Chama o agente **fora de qualquer transação** (o LLM leva segundos) e
    devolve a decisão serializável.

    Reaproveita a decisão já gravada: um retry não pergunta de novo ao modelo
    nem muda o que foi decidido antes.
    """
    if evento.get("decision"):
        try:
            d = json.loads(evento["decision"])
            logger.info(f"[inbox] evento {evento['id']} reaproveitando decisão "
                        f"já gravada ({d.get('action')})")
            return d
        except Exception:
            logger.warning(f"[inbox] decisão ilegível no evento {evento['id']} — "
                           f"recalculando")
    return agent_mod.decidir(
        tenant, evento["phone"], evento["body"],
        event_row_id=evento["id"],
        attempt_token=evento["attempt_token"],
        # Sem id do provedor a proteção por evento não vale: uma reentrega da
        # mesma mensagem vira outro evento. O agente então consulta, acolhe e
        # esclarece, mas não executa mutação (ver agent._mutacao_permitida).
        identidade_confiavel=(evento.get("event_id_source") == "provider"))


def _registrar_saida(tenant: dict, evento: dict, reply: str,
                     offer_id: str | None) -> int:
    """Enfileira a resposta. A chave de idempotência amarra a mensagem ao
    evento, então reprocessar não gera uma segunda resposta."""
    chave = f"inbound:{evento['id']}:reply"
    oid = db.outbox_enqueue(tenant["id"], evento["phone"], reply, chave,
                            kind="reply", event_row_id=evento["id"],
                            offer_id=offer_id)
    if offer_id:
        db.offer_set_outbox(offer_id, oid)
    return oid


def _registrar_avisos(tenant: dict, evento: dict, decisao: dict, rid: int) -> None:
    """Prepara os avisos à profissional no outbox. Deixa a exceção subir."""
    import asyncio as _aio
    import main as _m
    _aio.run(_m._handle_message(tenant, evento["phone"], evento["body"],
                                decisao=decisao, event_row_id=rid))


def _marcar_avisado(rid: int, tok: str) -> bool:
    """Marca que todas as intenções de aviso já estão persistidas."""
    return db.inbound_update(rid, tok, stage="avisado")


def process_event(evento: dict, agent_mod=None) -> dict:
    """Processa um evento já reivindicado. Devolve um resumo do desfecho.

    Cada avanço de estágio é gravado com o `attempt_token`. Se a posse foi
    perdida no meio (lease vencido, outro worker assumiu), a gravação falha e
    este worker **para** sem alterar mais nada.
    """
    if agent_mod is None:
        import agent as agent_mod
    rid, tok = evento["id"], evento["attempt_token"]
    tenant = _carregar_tenant(evento)
    if not tenant:
        db.inbound_update(rid, tok, state="failed", stage="failed",
                          last_error="consultório inexistente")
        return {"id": rid, "state": "failed", "motivo": "tenant_inexistente"}

    try:
        if _e_midia(evento["body"]):
            # Delegado ao fluxo legado, que já sabe responder mídia. Envio
            # direto, fora do outbox — ver EVIDENCIAS/03.md (limite, E05).
            import asyncio as _aio
            import main as _m
            _aio.run(_m._handle_message(tenant, evento["phone"], evento["body"]))
            db.inbound_update(rid, tok, state="done", stage="legacy_midia",
                              last_error="mídia tratada pelo fluxo legado (E05)")
            return {"id": rid, "state": "done", "motivo": "midia_legado"}

        motivo = _deve_ignorar(tenant, evento["phone"], evento["body"])
        if motivo:
            db.inbound_update(rid, tok, state="done", stage="skipped",
                              last_error=motivo)
            logger.info(f"[inbox] evento {rid} encerrado sem resposta: {motivo}")
            return {"id": rid, "state": "done", "motivo": motivo}

        # 1) Decisão — fora de transação, reaproveitada em retry.
        decisao = _decidir(tenant, evento, agent_mod)
        if not db.inbound_update(rid, tok, stage="decided",
                                 decision=json.dumps(decisao, ensure_ascii=False)):
            logger.warning(f"[inbox] posse perdida no evento {rid} (decided)")
            return {"id": rid, "state": "perdeu_posse"}

        # 2) Mutação local + resposta. A idempotência é por evento: efeitos
        #    gravados em `applied_effects` na mesma transação da mutação.
        resultado = agent_mod.aplicar(tenant, evento["phone"], decisao,
                                      event_row_id=rid)
        if not db.inbound_update(rid, tok, stage="mutated",
                                 reply_text=resultado["reply"],
                                 offer_id=resultado.get("offer_id")):
            logger.warning(f"[inbox] posse perdida no evento {rid} (mutated)")
            return {"id": rid, "state": "perdeu_posse"}

        # 3) Outbox: resposta ao paciente.
        _registrar_saida(tenant, evento, resultado["reply"],
                         resultado.get("offer_id"))
        if not db.inbound_update(rid, tok, stage="enqueued"):
            logger.warning(f"[inbox] posse perdida no evento {rid} (enqueued)")
            return {"id": rid, "state": "perdeu_posse"}

        # 4) Avisos à profissional — também efeitos do processamento, também no
        #    outbox, com chave por intenção.
        #
        #    A falha aqui **não** é engolida. Antes o erro ia para o log e o
        #    evento era concluído como `done`: um aviso que não chegou a ser
        #    preparado desaparecia sem nova tentativa. Agora a exceção sobe, o
        #    evento volta à fila com backoff e a retomada não repete mutação,
        #    resposta nem aviso já enfileirado (todos têm chave por evento).
        if evento.get("stage") != "avisado":
            _registrar_avisos(tenant, evento, decisao, rid)
            if not _marcar_avisado(rid, tok):
                logger.warning(f"[inbox] posse perdida no evento {rid} (avisado)")
                return {"id": rid, "state": "perdeu_posse"}

        # 5) Estado final: só agora, com todas as intenções persistidas.
        if not db.inbound_update(rid, tok, state="done", stage="finished"):
            logger.warning(f"[inbox] posse perdida no evento {rid} (finished)")
            return {"id": rid, "state": "perdeu_posse"}
        return {"id": rid, "state": "done", "action": decisao.get("action")}

    except Exception as e:
        atraso = _backoff(int(evento.get("attempts") or 1))
        logger.exception(f"[inbox] evento {rid} falhou: {e} — nova tentativa "
                         f"em {atraso}s")
        db.inbound_release(rid, tok, atraso, f"{type(e).__name__}: {e}",
                           MAX_ATTEMPTS)
        return {"id": rid, "state": "retry", "erro": str(e)}


# ── Laço ──────────────────────────────────────────────────────────────────────

def drain(worker: str | None = None, limit: int = 10, agent_mod=None) -> list[dict]:
    """Processa até `limit` eventos prontos. Seguro para chamar de qualquer
    lugar: a posse é por lease, então duas execuções simultâneas não colidem."""
    worker = worker or nome_worker()
    saida = []
    for _ in range(max(1, limit)):
        tomados = db.inbound_claim(worker, lease_sec=LEASE_SEC, limit=1)
        if not tomados:
            break
        saida.append(process_event(tomados[0], agent_mod=agent_mod))
    return saida


_parar = threading.Event()


def _loop():
    intervalo = max(2, getattr(config, "INBOX_WORKER_INTERVAL_SEC", 10))
    lote = max(1, getattr(config, "INBOX_BATCH", 20))
    while not _parar.is_set():
        try:
            feitos = drain(limit=lote)
            if feitos:
                logger.info(f"[inbox] {len(feitos)} evento(s) processado(s)")
        except Exception as e:
            logger.exception(f"[inbox] erro no laço: {e}")
        _parar.wait(intervalo)
    logger.info("[inbox] worker encerrado")


_thread = None


def stop_worker():
    """Pede o encerramento do laço. O lease garante que um evento em
    processamento não fica preso: ele volta à fila quando o prazo vence."""
    _parar.set()


def start_worker():
    """Thread no próprio processo, como o agendador. Não acrescenta serviço
    externo: a stack já é FastAPI + SQLite numa réplica.

    Protegido contra início duplicado: chamar duas vezes não cria duas threads.
    """
    global _thread
    if not getattr(config, "INBOX_WORKER_ENABLED", True):
        logger.info("[inbox] worker desativado (INBOX_WORKER_ENABLED=0)")
        return
    if _thread is not None and _thread.is_alive():
        logger.info("[inbox] worker já estava rodando — não duplicando")
        return
    _parar.clear()
    t = threading.Thread(target=_loop, daemon=True, name="inbox-worker")
    t.start()
    _thread = t
    logger.info(f"[inbox] worker iniciado (intervalo "
                f"{getattr(config, 'INBOX_WORKER_INTERVAL_SEC', 10)}s, "
                f"lease {LEASE_SEC}s, até {MAX_ATTEMPTS} tentativas)")
