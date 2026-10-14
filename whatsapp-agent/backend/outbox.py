"""Envio rastreável das respostas ao paciente (E03).

Distinção que este módulo existe para manter:

- **aceite do provedor** (`provider_accepted`): o HTTP do `sendText` voltou 2xx.
  É a melhor evidência disponível hoje. **Não** prova que o paciente recebeu.
- **entrega comprovada** (`delivery_confirmed`): o provedor confirmou a entrega
  ao aparelho. Exige webhook de status, que **não está configurado** nesta
  instalação — o estado existe, mas hoje nunca é atingido. Não inventamos
  consulta de status que a Evolution desta versão não oferece.
- **incerto** (`uncertain`): timeout ou queda depois de a requisição sair. A
  mensagem pode ter sido entregue. **Não se reenvia às cegas**: fica pendência
  auditável para o suporte resolver.

O estado do envio é o que decide se uma oferta de horários pode ser usada para
resolver "quero o 1" — ver `database.offer_mark_sent`.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time

import config
import database as db

logger = logging.getLogger(__name__)

LEASE_SEC = getattr(config, "OUTBOX_LEASE_SEC", 90)
MAX_ATTEMPTS = getattr(config, "OUTBOX_MAX_ATTEMPTS", 4)
_BACKOFF = (20, 90, 300, 900)

# Falhas que indicam "pode ter sido enviado": a requisição saiu e não sabemos o
# desfecho. Tratar como recusa levaria a reenvio duplicado.
_INCERTO = ("timeout", "timed out", "readtimeout", "connecttimeout",
            "connection reset", "connection aborted", "remotedisconnected")


def _backoff(tentativas: int) -> int:
    return _BACKOFF[min(max(tentativas, 1) - 1, len(_BACKOFF) - 1)]


def nome_worker() -> str:
    return f"{socket.gethostname()}:{threading.get_ident()}"


def _classificar(ok: bool, motivo: str) -> tuple[str, str]:
    """Traduz o resultado do provedor em estado de outbox."""
    if ok:
        return "provider_accepted", ""
    baixo = (motivo or "").lower()
    if any(t in baixo for t in _INCERTO):
        return "uncertain", motivo
    return "failed", motivo


def _enviar_real(tenant: dict, phone: str, body: str) -> tuple[str, str, str]:
    """Envia pelo provedor do consultório. Devolve (estado, id_do_provedor, erro).

    Nunca levanta: uma exceção aqui é tratada como **incerta**, porque não se
    sabe se a requisição chegou.
    """
    import whatsapp_service as wa
    try:
        ok, motivo = asyncio.run(wa.send_message_ex(tenant, phone, body))
    except Exception as e:
        return "uncertain", "", f"{type(e).__name__}: {e}"
    estado, erro = _classificar(ok, motivo)
    return estado, "", erro


def send_one(linha: dict, sender=None, max_attempts: int = MAX_ATTEMPTS) -> dict:
    """Envia uma linha já reivindicada e grava o desfecho nela e na oferta
    associada."""
    oid, tok = linha["id"], linha["attempt_token"]
    tenant = db.get_tenant_by_id(linha["tenant_id"])
    if not tenant:
        db.outbox_mark(oid, tok, "failed", last_error="consultório inexistente")
        return {"id": oid, "state": "failed"}

    enviar = sender or _enviar_real
    try:
        estado, msg_id, erro = enviar(tenant, linha["phone"], linha["body"])
    except Exception as e:
        # A exceção veio do próprio envio: não se sabe se a mensagem saiu.
        estado, msg_id, erro = "uncertain", "", f"{type(e).__name__}: {e}"

    if estado in ("provider_accepted", "delivery_confirmed"):
        db.outbox_mark(oid, tok, estado, provider=tenant.get("whatsapp_provider", ""),
                       provider_message_id=msg_id or "", last_error="")
        final = estado
    elif estado == "uncertain":
        # Pendência auditável: nunca reenviada automaticamente.
        db.outbox_mark(oid, tok, "uncertain",
                       provider=tenant.get("whatsapp_provider", ""),
                       provider_message_id=msg_id or "", last_error=erro)
        logger.warning(f"[outbox] envio {oid} INCERTO ({erro}) — não será "
                       f"reenviado automaticamente; resolver pelo suporte")
        final = "uncertain"
    else:
        final = db.outbox_requeue(oid, tok, _backoff(int(linha.get("attempts") or 1)),
                                  erro, max_attempts)
        logger.warning(f"[outbox] envio {oid} recusado ({erro}) → {final}")

    # A oferta associada recebe o desfecho **dela**, pelo offer_id — nunca por
    # (tenant, telefone), que marcaria a oferta errada depois de uma troca.
    if linha.get("offer_id") and final != "pending":
        r = db.offer_mark_sent(linha["offer_id"], final)
        logger.info(f"[outbox] oferta {linha['offer_id'][:8]} → {final} "
                    f"({r.get('acao')})")
    return {"id": oid, "state": final, "erro": erro}


def flush(worker: str | None = None, limit: int = 10, sender=None,
          max_attempts: int = MAX_ATTEMPTS) -> list[dict]:
    """Envia até `limit` mensagens pendentes. `limit` é o controle de vazão:
    uma fila grande não vira uma rajada contra o provedor."""
    worker = worker or nome_worker()
    linhas = db.outbox_claim(worker, lease_sec=LEASE_SEC, limit=max(1, limit))
    return [send_one(l, sender=sender, max_attempts=max_attempts) for l in linhas]


_parar = threading.Event()


def _loop():
    intervalo = max(2, getattr(config, "OUTBOX_WORKER_INTERVAL_SEC", 5))
    lote = max(1, getattr(config, "OUTBOX_BATCH", 10))
    while not _parar.is_set():
        try:
            feitos = flush(limit=lote)
            if feitos:
                logger.info(f"[outbox] {len(feitos)} envio(s) processado(s)")
        except Exception as e:
            logger.exception(f"[outbox] erro no laço: {e}")
        _parar.wait(intervalo)
    logger.info("[outbox] worker encerrado")


_thread = None


def stop_worker():
    _parar.set()


def start_worker():
    """Protegido contra início duplicado."""
    global _thread
    if not getattr(config, "OUTBOX_WORKER_ENABLED", True):
        logger.info("[outbox] worker desativado (OUTBOX_WORKER_ENABLED=0)")
        return
    if _thread is not None and _thread.is_alive():
        logger.info("[outbox] worker já estava rodando — não duplicando")
        return
    _parar.clear()
    t = threading.Thread(target=_loop, daemon=True, name="outbox-worker")
    t.start()
    _thread = t
    logger.info(f"[outbox] worker iniciado (intervalo "
                f"{getattr(config, 'OUTBOX_WORKER_INTERVAL_SEC', 5)}s, "
                f"lease {LEASE_SEC}s, até {MAX_ATTEMPTS} tentativas)")
