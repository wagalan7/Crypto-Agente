"""Convivência manual/bot na MESMA conta — reconhecimento e guard de propriedade.

Decisão de negócio (não reinterpretar aqui): uma posição aberta MANUALMENTE pelo
operador, quando reconhecida explicitamente por um administrador, deixa de
consumir slots/orçamento NOMINAL do bot, e o bot passa a poder avaliar OUTROS
símbolos pelos seus próprios limites. Em troca:

- o SÍMBOLO inteiro da posição manual fica indisponível para automação (inclusive
  direção contrária e hedge);
- o bot NÃO administra a posição manual (alavancagem, margem, qty, stop/TP,
  fechamento e cancelamento ficam fora);
- equity, saldo disponível e margem continuam sendo os REAIS da conta — nada de
  banca virtual, e a soma do risco manual com o automático PODE ultrapassar o
  limite nominal do bot (isto não isola o risco financeiro da conta).

Este módulo concentra: identidade (fingerprint), reconhecimento administrativo,
revalidação contra leitura fresca e o GUARD ÚNICO de propriedade consultado por
todos os caminhos mutantes. Não cria reconciliador, worker, fila ou motor de
risco: reutiliza sessão, locks e o reconciliador oficiais.

Limitação assumida e documentada: existe uma janela entre a última leitura e uma
ação que o operador faça direto na Binance. A advisory lock deste processo NÃO
impede o usuário de operar pela corretora. O contrato aqui é detectar divergência,
conter NOVAS entradas e nunca "corrigir" fechando/alterando posição alheia.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional

log = logging.getLogger(__name__)

EXCHANGE_BINANCE = "binance"
MARKET_USDM_FUTURES = "usdm_futures"

#: Idade MÁXIMA (s) entre a leitura fresca da exchange (feita FORA da transação)
#: e a decisão tomada DENTRO dela. Leitura mais velha não autoriza nada.
ACK_MAX_READ_AGE_S = 20.0

#: Motivos de RECUSA do reconhecimento (todos auditáveis, nenhum secreto).
ACK_OK = "MANUAL_ACK_OK"
ACK_IDEMPOTENT = "MANUAL_ACK_IDEMPOTENT"
ACK_NO_ACCOUNT = "MANUAL_ACK_NO_ACCOUNT"
ACK_POSITION_UNKNOWN = "MANUAL_ACK_POSITION_UNKNOWN"
ACK_POSITION_ABSENT = "MANUAL_ACK_POSITION_ABSENT"
ACK_AMBIGUOUS_LEGS = "MANUAL_ACK_AMBIGUOUS_LEGS"
ACK_IDENTITY_INCOMPLETE = "MANUAL_ACK_IDENTITY_INCOMPLETE"
ACK_FINGERPRINT_MISMATCH = "MANUAL_ACK_FINGERPRINT_MISMATCH"
ACK_CONFIRM_NOT_LITERAL = "MANUAL_ACK_CONFIRM_NOT_LITERAL"
ACK_READ_TOO_OLD = "MANUAL_ACK_READ_TOO_OLD"
ACK_BOT_TRADE_PRESENT = "MANUAL_ACK_BOT_TRADE_PRESENT"
ACK_INTENT_PENDING = "MANUAL_ACK_INTENT_PENDING"
ACK_INCIDENT_CONFLICT = "MANUAL_ACK_INCIDENT_CONFLICT"
ACK_BOT_ORDERS_PRESENT = "MANUAL_ACK_BOT_ORDERS_PRESENT"
ACK_ORDERS_UNKNOWN = "MANUAL_ACK_ORDERS_UNKNOWN"
ACK_DB_UNAVAILABLE = "MANUAL_ACK_DB_UNAVAILABLE"
ACK_CONFLICTING_ACTIVE = "MANUAL_ACK_CONFLICTING_ACTIVE"

#: Motivos do GUARD de propriedade.
GUARD_OK = "OWNERSHIP_OK"
GUARD_MANUAL_SYMBOL = "MANUAL_POSITION_SYMBOL_BLOCKED"
GUARD_REGISTRY_UNAVAILABLE = "MANUAL_ACK_REGISTRY_UNAVAILABLE"
GUARD_SYMBOL_UNKNOWN = "MANUAL_OWNERSHIP_SYMBOL_UNKNOWN"

_QUOTES = ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH", "BNB")


# ════════════════════════════════════════════════════════════════════════════
#  Normalização e identidade
# ════════════════════════════════════════════════════════════════════════════
def symbol_key(raw: Any) -> str:
    """Chave base/quote que DISTINGUE a quote (BTCUSDC ≠ BTCUSDT).

    Aceita as três formas que circulam no projeto: `BTC/USDT:USDT` (mercado),
    `BTCUSDT` (exchange) e `BTC-USDT-USDT` (identidade persistida da intenção).
    Forma irreconhecível volta normalizada, nunca adivinhada.
    """
    texto = str(raw or "").upper().strip()
    if not texto:
        return ""
    if "-" in texto and "/" not in texto and ":" not in texto:
        partes = texto.rsplit("-", 1)
        if len(partes) == 2 and "-" in partes[0]:
            texto = partes[0].replace("-", "/", 1) + ":" + partes[1]
    texto = re.sub(r"[^A-Z0-9/:]", "", texto)
    if "/" in texto:
        base = texto.split("/")[0]
        quote = texto.split("/")[1].split(":")[0]
        return f"{base}/{quote}" if base and quote else texto
    for quote in _QUOTES:
        if texto.endswith(quote) and len(texto) > len(quote):
            return f"{texto[:-len(quote)]}/{quote}"
    return texto


def canonical_symbol(raw: Any) -> str:
    """`BTC/USDT:USDT` — forma canônica de mercado usada no registro."""
    chave = symbol_key(raw)
    if "/" not in chave:
        return chave
    return f"{chave}:{chave.split('/')[1]}"


def quote_of(raw: Any) -> str:
    chave = symbol_key(raw)
    return chave.split("/")[1] if "/" in chave else ""


def explicit_side(raw: Any) -> Optional[str]:
    """`buy`/`sell` EXPLÍCITO. Ausente, inválido ou desconhecido → None.

    Nunca assume BUY por omissão: lado ausente invalida a identidade.
    """
    texto = str(raw or "").strip().lower()
    if texto in ("buy", "long"):
        return "buy"
    if texto in ("sell", "short"):
        return "sell"
    return None


def explicit_position_side(raw: Any) -> Optional[str]:
    """`BOTH`/`LONG`/`SHORT` REAL da exchange. Ausente/inválido → None.

    Campo ausente NÃO vira `BOTH`: em conta hedge isso trocaria a identidade da
    perna reconhecida.
    """
    texto = str(raw or "").strip().upper()
    return texto if texto in ("BOTH", "LONG", "SHORT") else None


def canonical_decimal(value: Any) -> Optional[Decimal]:
    """Decimal canônico e finito. Recusa bool, NaN, infinito e não-numérico.

    Decimais canônicos evitam que `0.10` e `0.1` produzam fingerprints
    diferentes para a MESMA posição.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        numero = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not numero.is_finite():
        return None
    normalizado = numero.normalize()
    # `1E+2` volta a `100`: a representação não pode depender do expoente.
    if normalizado == normalizado.to_integral_value():
        try:
            normalizado = normalizado.quantize(Decimal(1))
        except InvalidOperation:
            pass
    return normalizado


def canonical_update_time_ms(value: Any) -> Optional[int]:
    """`updateTime` REAL da exchange, em ms. Ausente/inválido/<=0 → None.

    Relógio local NUNCA substitui este campo: sem versão temporal observável a
    identidade não é verificável e o reconhecimento é recusado.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        inteiro = int(value)
    except (TypeError, ValueError):
        return None
    return inteiro if inteiro > 0 else None


def position_fingerprint(*, account_scope: str, exchange: str, market: str,
                         symbol: Any, side: Any, position_side: Any,
                         qty: Any, entry_price: Any, update_time_ms: Any,
                         contract_version: str) -> Optional[str]:
    """Identidade determinística da posição observada.

    Entram: conta, mercado, símbolo/quote, lado, positionSide, qty, preço de
    entrada e a versão temporal OBSERVÁVEL da exchange. NÃO entram mark price
    nem P&L — a oscilação deles não exige novo reconhecimento.

    Qualquer parcela ausente/inválida devolve None: geometria inválida não vira
    fingerprint "quase certo".
    """
    chave = symbol_key(symbol)
    lado = explicit_side(side)
    perna = explicit_position_side(position_side)
    quantidade = canonical_decimal(qty)
    preco = canonical_decimal(entry_price)
    versao = canonical_update_time_ms(update_time_ms)
    conta = str(account_scope or "").strip()
    if (not conta or "/" not in chave or lado is None or perna is None
            or quantidade is None or preco is None or versao is None):
        return None
    if quantidade <= 0 or preco <= 0:
        return None
    corpo = {
        "contract_version": str(contract_version),
        "account_scope": conta,
        "exchange": str(exchange or "").strip().lower(),
        "market": str(market or "").strip().lower(),
        "symbol": chave,
        "quote": chave.split("/")[1],
        "side": lado,
        "position_side": perna,
        "qty": str(quantidade),
        "entry_price": str(preco),
        "exchange_update_time_ms": versao,
    }
    bruto = json.dumps(corpo, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_ms() -> int:
    return int(_now().timestamp() * 1000)


# ════════════════════════════════════════════════════════════════════════════
#  Leitura fresca da exchange (SEMPRE fora de transação/lock)
# ════════════════════════════════════════════════════════════════════════════
def current_account_scope() -> Optional[str]:
    """Conta vigente pelo MESMO contrato opaco do ledger (R05C/R05D)."""
    try:
        from services.financial_total_service import current_account_scope as _scope
        escopo = _scope()
    except Exception:  # noqa: BLE001
        return None
    return escopo if isinstance(escopo, str) and escopo.strip() else None


async def observe_positions(symbol: Optional[str] = None) -> Dict[str, Any]:
    """Leitura FRESCA das posições, normalizada para a identidade.

    Stale, rate-limited, erro ou fonte não configurada ⇒ `ok=False` com motivo:
    fonte incerta NUNCA autoriza reconhecimento nem liberação.
    """
    try:
        from services import binance_signed_service as bss
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason_code": ACK_POSITION_UNKNOWN,
                "detail": type(exc).__name__, "positions": [], "observed_at_ms": None}
    try:
        if not bss.is_configured():
            return {"ok": False, "reason_code": ACK_POSITION_UNKNOWN,
                    "detail": "exchange não configurada", "positions": [],
                    "observed_at_ms": None}
        res = await bss.get_positions(symbol, force=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason_code": ACK_POSITION_UNKNOWN,
                "detail": type(exc).__name__, "positions": [], "observed_at_ms": None}
    if not isinstance(res, dict) or not res.get("ok") or res.get("stale") \
            or res.get("rate_limited"):
        return {"ok": False, "reason_code": ACK_POSITION_UNKNOWN,
                "detail": "leitura stale/rate-limited/indisponível", "positions": [],
                "observed_at_ms": None}
    linhas = normalize_positions(res.get("positions"))
    return {"ok": True, "reason_code": "POSITIONS_FRESH", "positions": linhas,
            "observed_at_ms": _now_ms()}


def normalize_positions(raw: Any) -> List[dict]:
    """Normaliza linhas JÁ lidas de `get_positions` (sem nova chamada HTTP).

    Reaproveitado pelo reconciliador: ele já fez a leitura fresca do ciclo e
    não pode gastar outra chamada só para revalidar reconhecimentos.
    """
    linhas = []
    for bruta in (raw or ()):
        if not isinstance(bruta, Mapping):
            continue
        quantidade = canonical_decimal(bruta.get("size"))
        if quantidade is None or quantidade <= 0:
            continue
        linhas.append({
            "symbol": canonical_symbol(bruta.get("symbol")),
            "symbol_key": symbol_key(bruta.get("symbol")),
            "quote": quote_of(bruta.get("symbol")),
            "side": explicit_side(bruta.get("side")),
            "position_side": explicit_position_side(bruta.get("position_side")),
            "qty": quantidade,
            "entry_price": canonical_decimal(bruta.get("entry_price")),
            "update_time_ms": canonical_update_time_ms(bruta.get("update_time_ms")),
        })
    return linhas


def _leg_for_symbol(positions: List[dict], chave: str) -> Dict[str, Any]:
    """Perna ÚNICA daquele símbolo. Várias pernas ⇒ ambíguo (nunca condensa).

    Condensar duas pernas numa posição fictícia inventaria uma identidade que a
    exchange não tem — neste pacote isso é RECUSA explícita.
    """
    pernas = [p for p in positions if p.get("symbol_key") == chave]
    if not pernas:
        return {"ok": False, "reason_code": ACK_POSITION_ABSENT, "position": None}
    if len(pernas) > 1:
        return {"ok": False, "reason_code": ACK_AMBIGUOUS_LEGS, "position": None,
                "legs": len(pernas)}
    return {"ok": True, "reason_code": "LEG_UNIQUE", "position": pernas[0]}


def describe_candidate(position: Mapping, *, account_scope: str) -> Dict[str, Any]:
    """Candidato para o GET administrativo, com o fingerprint de confirmação."""
    impressao = position_fingerprint(
        account_scope=account_scope, exchange=EXCHANGE_BINANCE,
        market=MARKET_USDM_FUTURES, symbol=position.get("symbol"),
        side=position.get("side"), position_side=position.get("position_side"),
        qty=position.get("qty"), entry_price=position.get("entry_price"),
        update_time_ms=position.get("update_time_ms"),
        contract_version=_contract_version())
    return {
        "symbol": position.get("symbol"),
        "quote": position.get("quote"),
        "side": position.get("side"),
        "position_side": position.get("position_side"),
        "qty": str(position.get("qty")) if position.get("qty") is not None else None,
        "entry_price": (str(position.get("entry_price"))
                        if position.get("entry_price") is not None else None),
        "exchange_update_time_ms": position.get("update_time_ms"),
        "fingerprint": impressao,
        "identity_complete": impressao is not None,
        "contract_version": _contract_version(),
    }


def _contract_version() -> str:
    from models.manual_position_ack import ACK_CONTRACT_VERSION
    return ACK_CONTRACT_VERSION


# ════════════════════════════════════════════════════════════════════════════
#  Registro persistido (leitura) e GUARD de propriedade
# ════════════════════════════════════════════════════════════════════════════
async def active_acknowledgements() -> Dict[str, Any]:
    """Reconhecimentos ACTIVE persistidos.

    Erro de leitura devolve `ok=False` — registro inacessível NÃO vira lista
    vazia (isso liberaria o símbolo manual) nem exceção solta no chamador.
    """
    try:
        from db import DB_ENABLED, get_session
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason_code": GUARD_REGISTRY_UNAVAILABLE,
                "detail": type(exc).__name__, "acks": []}
    if not DB_ENABLED:
        # Sem banco não existe reconhecimento: nenhum símbolo é isento, e o
        # resto do sistema já bloqueia entradas por falta de intenção.
        return {"ok": True, "reason_code": "REGISTRY_DISABLED", "acks": []}
    try:
        from models.manual_position_ack import ManualPositionAcknowledgement as Ack
        from models.manual_position_ack import STATE_ACTIVE
        from sqlalchemy import select
        async with get_session() as session:
            linhas = (await session.execute(
                select(Ack).where(Ack.state == STATE_ACTIVE))).scalars().all()
            return {"ok": True, "reason_code": "REGISTRY_READ",
                    "acks": [linha.to_public() for linha in linhas]}
    except Exception as exc:  # noqa: BLE001
        log.error(f"[manual-ack] registro inacessível: {type(exc).__name__}: {exc}")
        return {"ok": False, "reason_code": GUARD_REGISTRY_UNAVAILABLE,
                "detail": type(exc).__name__, "acks": []}


async def ownership_guard(symbol: Any = None, *, action: str = "mutate") -> Dict[str, Any]:
    """GUARD ÚNICO: o bot pode tocar neste símbolo/conta?

    Contrato fail-closed:

    - registro ilegível ⇒ BLOQUEIA (não sabemos se o símbolo é manual);
    - símbolo reconhecido como manual ⇒ BLOQUEIA (inclusive direção contrária
      e hedge — neste pacote o símbolo inteiro fica indisponível);
    - símbolo DESCONHECIDO pelo chamador, havendo algum reconhecimento ativo
      ⇒ BLOQUEIA (prova inconclusiva não autoriza mutação);
    - sem reconhecimento ativo ⇒ LIBERA (comportamento anterior preservado).

    NÃO consulta a exchange: é chamado de dentro de locks/semáforos do próprio
    transporte, onde I/O HTTP recursivo é proibido.
    """
    registro = await active_acknowledgements()
    if not registro["ok"]:
        return {"allowed": False, "reason_code": GUARD_REGISTRY_UNAVAILABLE,
                "action": action, "symbol": None,
                "detail": registro.get("detail")}
    acks = registro["acks"]
    if not acks:
        return {"allowed": True, "reason_code": GUARD_OK, "action": action,
                "symbol": (canonical_symbol(symbol) if symbol else None)}
    chave = symbol_key(symbol)
    if not chave:
        return {"allowed": False, "reason_code": GUARD_SYMBOL_UNKNOWN,
                "action": action, "symbol": None,
                "detail": "símbolo não identificado com reconhecimento manual ativo"}
    for ack in acks:
        if symbol_key(ack.get("symbol")) == chave:
            return {"allowed": False, "reason_code": GUARD_MANUAL_SYMBOL,
                    "action": action, "symbol": ack.get("symbol"),
                    "ack_id": ack.get("id"), "side": ack.get("side"),
                    "detail": "símbolo com posição manual reconhecida"}
    return {"allowed": True, "reason_code": GUARD_OK, "action": action,
            "symbol": canonical_symbol(symbol)}


async def blocked_symbol_keys() -> Dict[str, Any]:
    """Chaves de símbolo indisponíveis para o bot (uso das coortes/contagens)."""
    registro = await active_acknowledgements()
    if not registro["ok"]:
        return {"ok": False, "reason_code": registro["reason_code"], "keys": set()}
    return {"ok": True, "reason_code": registro["reason_code"],
            "keys": {symbol_key(a.get("symbol")) for a in registro["acks"]}}


# ════════════════════════════════════════════════════════════════════════════
#  Provas BOT do símbolo (lidas no banco; a exchange fica FORA da transação)
# ════════════════════════════════════════════════════════════════════════════
def _known_bot_identities(real_trades, intents, incidents) -> Dict[str, set]:
    """IDs e prefixos que pertencem COMPROVADAMENTE ao bot naquele símbolo."""
    exatos, prefixos = set(), set()
    for linha in real_trades or ():
        for campo in ("client_order_id", "exchange_order_id", "sl_order_id",
                      "tp1_order_id", "tp2_order_id"):
            valor = linha.get(campo)
            if valor:
                exatos.add(str(valor))
        if linha.get("client_order_id"):
            prefixos.add(str(linha["client_order_id"]))
    for linha in intents or ():
        if linha.get("client_order_id"):
            exatos.add(str(linha["client_order_id"]))
            prefixos.add(str(linha["client_order_id"]))
        for despachado in (linha.get("dispatch_ids") or ()):
            if despachado:
                exatos.add(str(despachado))
    for linha in incidents or ():
        for campo in ("client_order_id", "entry_order_id"):
            if linha.get(campo):
                exatos.add(str(linha[campo]))
        prefixo = linha.get("conditional_prefix")
        if prefixo:
            prefixos.add(str(prefixo))
        ids = linha.get("conditional_ids")
        if isinstance(ids, Mapping):
            for chave in ("sl", "tp1", "tp2"):
                if ids.get(chave):
                    exatos.add(str(ids[chave]))
            for chave in ("all", "ids"):
                if isinstance(ids.get(chave), list):
                    exatos.update(str(x) for x in ids[chave] if x)
    return {"exact": exatos, "prefixes": prefixos}


def _order_matches_bot(order: Mapping, identities: Mapping) -> bool:
    exatos = identities.get("exact") or set()
    prefixos = identities.get("prefixes") or set()
    vistos = {str(order.get(campo)) for campo in
              ("algo_id", "client_algo_id", "order_id", "client_order_id",
               "orderId", "clientOrderId", "algoId", "clientAlgoId")
              if order.get(campo)}
    if vistos & exatos:
        return True
    for identificador in vistos:
        for prefixo in prefixos:
            if identificador.startswith(f"{prefixo}-"):
                return True
    return False


async def _bot_state_for_symbol(session, chave: str, *, observed_at_ms: Optional[int]):
    """Provas BOT daquele símbolo lidas DENTRO da transação da confirmação.

    Devolve `(motivo_de_recusa | None, detalhe)`. Qualquer impedimento aqui
    recusa o reconhecimento: ausência de `RealTrade` NÃO significa origem
    manual, então a prova precisa ser de AUSÊNCIA de rastro automático.
    """
    from models.entry_intent import EntryIntent, PENDING_STATES
    from models.execution_incident import ExecutionIncident
    from models.real_trade import RealTrade
    from sqlalchemy import select

    colunas = (RealTrade.id, RealTrade.symbol, RealTrade.source, RealTrade.status,
               RealTrade.client_order_id, RealTrade.exchange_order_id,
               RealTrade.sl_order_id, RealTrade.tp1_order_id, RealTrade.tp2_order_id)
    abertos = (await session.execute(
        select(*colunas).where(RealTrade.status == "open"))).mappings().all()
    automaticos = [dict(linha) for linha in abertos
                   if symbol_key(linha.get("symbol")) == chave
                   and str(linha.get("source") or "").lower() in ("auto", "managed")]
    if automaticos:
        return ACK_BOT_TRADE_PRESENT, {"real_trade_ids": [l["id"] for l in automaticos]}
    # Identidades do BOT naquele símbolo incluem trades JÁ FECHADOS: uma
    # condicional órfã sobrevive ao fechamento do registro, e atribuí-la ao
    # operador por engano liberaria o reconhecimento sobre ordem do bot.
    historicos = (await session.execute(
        select(*colunas).where(RealTrade.symbol == canonical_symbol(chave))
        .order_by(RealTrade.id.desc()).limit(200))).mappings().all()
    do_simbolo = [dict(linha) for linha in historicos]
    vistos = {l["id"] for l in do_simbolo}
    do_simbolo.extend(dict(linha) for linha in abertos
                      if symbol_key(linha.get("symbol")) == chave
                      and linha.get("id") not in vistos)

    intencoes = (await session.execute(
        select(EntryIntent.intent_key, EntryIntent.symbol, EntryIntent.state,
               EntryIntent.client_order_id, EntryIntent.dispatch_ids,
               EntryIntent.real_trade_id, EntryIntent.updated_at))).mappings().all()
    intencoes = [dict(linha) for linha in intencoes
                 if symbol_key(linha.get("symbol")) == chave]
    pendentes = [linha for linha in intencoes
                 if str(linha.get("state")) in set(PENDING_STATES)]
    if pendentes:
        return ACK_INTENT_PENDING, {"intent_keys": [l["intent_key"] for l in pendentes]}
    # Reserva CONCORRENTE: qualquer intenção daquele símbolo tocada depois da
    # leitura usada nesta confirmação invalida a leitura (ela é anterior).
    if observed_at_ms is not None:
        for linha in intencoes:
            tocada = linha.get("updated_at")
            if isinstance(tocada, datetime):
                quando = tocada if tocada.tzinfo else tocada.replace(tzinfo=timezone.utc)
                if int(quando.timestamp() * 1000) >= int(observed_at_ms):
                    return ACK_INTENT_PENDING, {"intent_keys": [linha["intent_key"]],
                                                "detail": "intenção tocada após a leitura"}

    incidentes = (await session.execute(
        select(ExecutionIncident.incident_key, ExecutionIncident.symbol,
               ExecutionIncident.kind, ExecutionIncident.state,
               ExecutionIncident.client_order_id, ExecutionIncident.entry_order_id,
               ExecutionIncident.conditional_prefix, ExecutionIncident.conditional_ids)
        .where(ExecutionIncident.resolved_at.is_(None)))).mappings().all()
    incidentes = [dict(linha) for linha in incidentes
                  if symbol_key(linha.get("symbol")) == chave]
    conflitantes = [linha for linha in incidentes
                    if str(linha.get("kind")) != "UNTRACKED_POSITION"]
    if conflitantes:
        return ACK_INCIDENT_CONFLICT, {
            "incident_keys": [l["incident_key"] for l in conflitantes]}
    return None, {"identities": _known_bot_identities(do_simbolo, intencoes, incidentes),
                  "untracked": [l for l in incidentes
                                if str(l.get("kind")) == "UNTRACKED_POSITION"]}


async def _open_bot_orders(chave: str, identities: Mapping) -> Dict[str, Any]:
    """Ordens condicionais ABERTAS do símbolo atribuíveis ao BOT.

    Leitura da exchange — roda FORA da transação. Listagem indisponível é prova
    INCONCLUSIVA e bloqueia: nunca se conclui ausência de ordem por erro.

    Ordens NÃO atribuíveis ao bot não bloqueiam o reconhecimento: proteções do
    operador podem existir, e reconhecer não certifica nem instala proteção.
    """
    try:
        from services import binance_signed_service as bss
        res = await bss.get_open_algo_orders(canonical_symbol(chave))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason_code": ACK_ORDERS_UNKNOWN,
                "detail": type(exc).__name__, "bot_orders": []}
    if not isinstance(res, dict) or not res.get("ok"):
        return {"ok": False, "reason_code": ACK_ORDERS_UNKNOWN,
                "detail": "listagem de condicionais indisponível", "bot_orders": []}
    doBot = []
    for ordem in (res.get("orders") or []):
        if not isinstance(ordem, Mapping):
            return {"ok": False, "reason_code": ACK_ORDERS_UNKNOWN,
                    "detail": "ordem em formato inesperado", "bot_orders": []}
        if symbol_key(ordem.get("symbol")) != chave:
            continue
        if _order_matches_bot(ordem, identities):
            doBot.append(str(ordem.get("algo_id") or ordem.get("client_algo_id") or "?"))
    return {"ok": True, "reason_code": "ORDERS_READ", "bot_orders": doBot,
            "open_count": len(res.get("orders") or [])}


async def symbol_has_live_orders(symbol: Any) -> Dict[str, Any]:
    """Há ordens/condicionais VIVAS naquele símbolo? Incerteza ⇒ `ok=False`.

    Usado após o fechamento comprovado da posição manual: enquanto restarem
    ordens do operador, o símbolo continua indisponível para o bot — e elas
    NÃO são canceladas.
    """
    chave = symbol_key(symbol)
    try:
        from services import binance_signed_service as bss
        res = await bss.get_open_algo_orders(canonical_symbol(chave))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason_code": ACK_ORDERS_UNKNOWN, "detail": type(exc).__name__}
    if not isinstance(res, dict) or not res.get("ok"):
        return {"ok": False, "reason_code": ACK_ORDERS_UNKNOWN,
                "detail": "listagem de condicionais indisponível"}
    vivas = [o for o in (res.get("orders") or [])
             if isinstance(o, Mapping) and symbol_key(o.get("symbol")) == chave]
    return {"ok": True, "reason_code": "ORDERS_READ", "live": len(vivas) > 0,
            "count": len(vivas)}


# ════════════════════════════════════════════════════════════════════════════
#  GET administrativo — candidatos + fingerprint de confirmação
# ════════════════════════════════════════════════════════════════════════════
async def list_candidates() -> Dict[str, Any]:
    """Posições frescas da conta com o fingerprint que o POST vai exigir.

    Sem segredo, sem credencial e sem stack trace: só identidade observável,
    estado do reconhecimento e por que cada símbolo está (ou não) elegível.
    """
    escopo = current_account_scope()
    if not escopo:
        return {"ok": False, "reason_code": ACK_NO_ACCOUNT,
                "detail": "conta não comprovada", "candidates": [], "active": []}
    leitura = await observe_positions()
    registro = await active_acknowledgements()
    ativos = registro["acks"] if registro["ok"] else []
    if not leitura["ok"]:
        return {"ok": False, "reason_code": leitura["reason_code"],
                "detail": leitura.get("detail"), "candidates": [],
                "active": ativos, "registry_ok": registro["ok"],
                "account_scope": escopo}
    por_simbolo: Dict[str, List[dict]] = {}
    for posicao in leitura["positions"]:
        por_simbolo.setdefault(posicao["symbol_key"], []).append(posicao)
    candidatos = []
    reconhecidos = {symbol_key(a.get("symbol")) for a in ativos}
    for chave, pernas in sorted(por_simbolo.items()):
        if len(pernas) > 1:
            candidatos.append({"symbol": canonical_symbol(chave), "eligible": False,
                               "reason_code": ACK_AMBIGUOUS_LEGS, "legs": len(pernas),
                               "fingerprint": None})
            continue
        descricao = describe_candidate(pernas[0], account_scope=escopo)
        descricao["already_acknowledged"] = chave in reconhecidos
        descricao["eligible"] = bool(descricao["identity_complete"])
        if not descricao["eligible"]:
            descricao["reason_code"] = ACK_IDENTITY_INCOMPLETE
        candidatos.append(descricao)
    return {"ok": True, "reason_code": "CANDIDATES_FRESH", "account_scope": escopo,
            "observed_at_ms": leitura["observed_at_ms"], "candidates": candidatos,
            "active": ativos, "registry_ok": registro["ok"],
            "contract_version": _contract_version(),
            "note": ("reconhecer NÃO autoriza entrada: o reconciliador oficial "
                     "ainda precisa validar segurança no ciclo dele")}


# ════════════════════════════════════════════════════════════════════════════
#  POST administrativo — reconhecimento explícito
# ════════════════════════════════════════════════════════════════════════════
def _refusal(reason_code: str, **extra) -> Dict[str, Any]:
    return {"ok": False, "acknowledged": False, "reason_code": reason_code, **extra}


async def acknowledge(*, symbol: Any, expected_fingerprint: Any, confirm: Any,
                      reason: Optional[str] = None,
                      identity_note: Optional[str] = None) -> Dict[str, Any]:
    """Reconhece EXPLICITAMENTE uma posição manual. Fail-closed em tudo.

    Ordem obrigatória: confirmação literal → conta → leitura FRESCA (fora de
    transação) → identidade idêntica ao fingerprint informado → ordens BOT
    abertas → transação sob a MESMA advisory lock da admissão de entrada
    (`917283`), onde as provas decisórias são relidas, a idade da leitura é
    validada e o registro nasce ATOMICAMENTE junto do vínculo com a causa
    UNTRACKED.

    Sucesso NÃO é autorização de entrada: o reconciliador oficial ainda precisa
    validar segurança e liberar a pausa pelo fluxo P03.
    """
    # `true` literal: a string "true", 1 ou "on" NÃO valem.
    if confirm is not True:
        return _refusal(ACK_CONFIRM_NOT_LITERAL,
                        detail="confirmação precisa ser o booleano true")
    escopo = current_account_scope()
    if not escopo:
        return _refusal(ACK_NO_ACCOUNT, detail="conta não comprovada")
    chave = symbol_key(symbol)
    if "/" not in chave:
        return _refusal(ACK_IDENTITY_INCOMPLETE, detail="símbolo inválido")
    esperado = str(expected_fingerprint or "").strip()
    if not esperado:
        return _refusal(ACK_FINGERPRINT_MISMATCH, detail="fingerprint não informado")

    # 1. Leitura FRESCA da posição — I/O da exchange fora de qualquer transação.
    leitura = await observe_positions(canonical_symbol(chave))
    if not leitura["ok"]:
        return _refusal(leitura["reason_code"], detail=leitura.get("detail"))
    perna = _leg_for_symbol(leitura["positions"], chave)
    if not perna["ok"]:
        return _refusal(perna["reason_code"], legs=perna.get("legs"))
    posicao = perna["position"]
    impressao = position_fingerprint(
        account_scope=escopo, exchange=EXCHANGE_BINANCE, market=MARKET_USDM_FUTURES,
        symbol=posicao["symbol"], side=posicao["side"],
        position_side=posicao["position_side"], qty=posicao["qty"],
        entry_price=posicao["entry_price"], update_time_ms=posicao["update_time_ms"],
        contract_version=_contract_version())
    if impressao is None:
        return _refusal(ACK_IDENTITY_INCOMPLETE,
                        detail="identidade incompleta (lado/positionSide/qty/entrada/updateTime)")
    if impressao != esperado:
        return _refusal(ACK_FINGERPRINT_MISMATCH, current_fingerprint=impressao)

    try:
        from db import DB_ENABLED, get_session
    except Exception as exc:  # noqa: BLE001
        return _refusal(ACK_DB_UNAVAILABLE, detail=type(exc).__name__)
    if not DB_ENABLED:
        return _refusal(ACK_DB_UNAVAILABLE, detail="banco desabilitado")

    # 2. Provas BOT do símbolo (identidades) — leitura auxiliar para a listagem.
    try:
        async with get_session() as session:
            motivo, detalhe = await _bot_state_for_symbol(
                session, chave, observed_at_ms=leitura["observed_at_ms"])
    except Exception as exc:  # noqa: BLE001
        return _refusal(ACK_DB_UNAVAILABLE, detail=type(exc).__name__)
    if motivo:
        return _refusal(motivo, **{k: v for k, v in detalhe.items() if k != "identities"})

    # 3. Ordens BOT abertas — exchange FORA da transação; incerteza bloqueia.
    ordens = await _open_bot_orders(chave, detalhe.get("identities") or {})
    if not ordens["ok"]:
        return _refusal(ordens["reason_code"], detail=ordens.get("detail"))
    if ordens["bot_orders"]:
        return _refusal(ACK_BOT_ORDERS_PRESENT, bot_orders=ordens["bot_orders"])

    # 4. Transação decisória sob a MESMA lock da admissão de entrada.
    return await _persist_acknowledgement(
        account_scope=escopo, chave=chave, posicao=posicao, fingerprint=impressao,
        observed_at_ms=leitura["observed_at_ms"], reason=reason,
        identity_note=identity_note, open_orders=ordens.get("open_count"))


async def _persist_acknowledgement(*, account_scope: str, chave: str, posicao: Mapping,
                                   fingerprint: str, observed_at_ms: int,
                                   reason: Optional[str], identity_note: Optional[str],
                                   open_orders: Optional[int]) -> Dict[str, Any]:
    from db import get_session
    from models.execution_incident import ExecutionIncident
    from models.manual_position_ack import (ACK_CONTRACT_VERSION, STATE_ACTIVE,
                                            ManualPositionAcknowledgement as Ack)
    from services.entry_intent_service import RISK_LOCK_KEY
    from sqlalchemy import select, text

    agora = _now()
    try:
        async with get_session() as session:
            async with session.begin():
                # MESMA lock da admissão: reconhecimento e reserva de entrada
                # são mutuamente exclusivos.
                await session.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                                      {"k": RISK_LOCK_KEY})
                # Idade da leitura validada DENTRO da transação: leitura velha
                # (ou anterior a uma reserva concorrente) não autoriza nada.
                idade_s = max(0.0, (_now_ms() - int(observed_at_ms)) / 1000.0)
                if idade_s > ACK_MAX_READ_AGE_S:
                    raise _AckRefused(ACK_READ_TOO_OLD, {"read_age_s": round(idade_s, 2)})
                motivo, detalhe = await _bot_state_for_symbol(
                    session, chave, observed_at_ms=observed_at_ms)
                if motivo:
                    raise _AckRefused(motivo, {k: v for k, v in detalhe.items()
                                               if k != "identities"})
                existente = (await session.execute(
                    select(Ack).where(Ack.account_scope == account_scope,
                                      Ack.exchange == EXCHANGE_BINANCE,
                                      Ack.market == MARKET_USDM_FUTURES,
                                      Ack.symbol == canonical_symbol(chave),
                                      Ack.state == STATE_ACTIVE)
                    .with_for_update())).scalar_one_or_none()
                if existente is not None:
                    if existente.fingerprint == fingerprint:
                        # Repetir a MESMA confirmação é idempotente.
                        return {"ok": True, "acknowledged": True, "idempotent": True,
                                "reason_code": ACK_IDEMPOTENT,
                                "acknowledgement": existente.to_public(),
                                "note": _POST_NOTE}
                    raise _AckRefused(ACK_CONFLICTING_ACTIVE,
                                      {"active_fingerprint": existente.fingerprint})
                # Vínculo ATÔMICO com a causa UNTRACKED daquele símbolo.
                untracked = [linha for linha in (detalhe.get("untracked") or ())]
                incident_key = untracked[0]["incident_key"] if len(untracked) == 1 else None
                registro = Ack(
                    account_scope=account_scope, exchange=EXCHANGE_BINANCE,
                    market=MARKET_USDM_FUTURES, symbol=canonical_symbol(chave),
                    quote=quote_of(chave), side=posicao["side"],
                    position_side=posicao["position_side"],
                    qty=posicao["qty"], entry_price=posicao["entry_price"],
                    exchange_update_time_ms=int(posicao["update_time_ms"]),
                    fingerprint=fingerprint, contract_version=ACK_CONTRACT_VERSION,
                    state=STATE_ACTIVE, reason=_short(reason, 200),
                    identity_note=_short(identity_note, 120),
                    incident_key=incident_key,
                    evidence={"observed_at_ms": int(observed_at_ms),
                              "read_age_s": round(idade_s, 2),
                              "open_conditional_orders": open_orders,
                              "untracked_incidents": [l["incident_key"] for l in untracked]},
                    created_at=agora, updated_at=agora, ended_at=None, ended_reason=None)
                session.add(registro)
                if incident_key:
                    incidente = (await session.execute(
                        select(ExecutionIncident)
                        .where(ExecutionIncident.incident_key == incident_key)
                        .with_for_update())).scalar_one_or_none()
                    if incidente is not None:
                        # O incidente CONTINUA aberto: quem encerra é o
                        # reconciliador, depois de validar contra leitura fresca.
                        incidente.manual_reason = (
                            "posição manual reconhecida pelo administrador; "
                            "aguardando validação do reconciliador")[:500]
                        atual = incidente.payload if isinstance(incidente.payload, dict) else {}
                        incidente.payload = {**atual, "manual_ack": {
                            "fingerprint": fingerprint,
                            "symbol": canonical_symbol(chave),
                            "acknowledged_at": agora.isoformat()}}
                        incidente.updated_at = agora
                await session.flush()
                publico = registro.to_public()
            return {"ok": True, "acknowledged": True, "idempotent": False,
                    "reason_code": ACK_OK, "acknowledgement": publico,
                    "note": _POST_NOTE}
    except _AckRefused as recusa:
        return _refusal(recusa.reason_code, **recusa.detail)
    except Exception as exc:  # noqa: BLE001
        log.error(f"[manual-ack] persistência falhou: {type(exc).__name__}: {exc}")
        return _refusal(ACK_DB_UNAVAILABLE, detail=type(exc).__name__)


_POST_NOTE = ("reconhecimento gravado; o reconciliador oficial ainda precisa "
              "validar segurança e liberar a pausa pelo fluxo P03 — este "
              "sucesso NÃO é autorização de entrada")


class _AckRefused(Exception):
    def __init__(self, reason_code: str, detail: Optional[dict] = None):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.detail = detail or {}


def _short(value: Any, limit: int) -> Optional[str]:
    """Texto administrativo curto e SEM segredo (nada de token/stack trace)."""
    if value is None:
        return None
    texto = re.sub(r"\s+", " ", str(value)).strip()
    return texto[:limit] or None


# ════════════════════════════════════════════════════════════════════════════
#  Revalidação — chamada pelo reconciliador oficial (boot e ciclos)
# ════════════════════════════════════════════════════════════════════════════
async def revalidate_active(*, positions: Optional[List[dict]] = None,
                            observed_ok: Optional[bool] = None) -> Dict[str, Any]:
    """Confere cada reconhecimento ACTIVE contra a leitura FRESCA da conta.

    - identidade idêntica ⇒ permanece ACTIVE;
    - qty/lado/positionSide/versão temporal/conta divergentes ⇒ INVALIDATED
      (a autorização anterior cai; é preciso novo reconhecimento);
    - posição comprovadamente ausente ⇒ CLOSED, preservando histórico e sem
      cancelar nenhuma ordem do operador;
    - leitura incerta ou registro ilegível ⇒ NADA muda e `ok=False`: UNKNOWN
      mantém o bloqueio, nunca libera.
    """
    registro = await active_acknowledgements()
    if not registro["ok"]:
        return {"ok": False, "reason_code": registro["reason_code"],
                "valid": [], "invalidated": [], "closed": []}
    if not registro["acks"]:
        return {"ok": True, "reason_code": "NO_ACTIVE_ACKS", "valid": [],
                "invalidated": [], "closed": []}
    if positions is None:
        leitura = await observe_positions()
        if not leitura["ok"]:
            return {"ok": False, "reason_code": leitura["reason_code"],
                    "valid": [], "invalidated": [], "closed": []}
        positions = leitura["positions"]
    elif observed_ok is False:
        return {"ok": False, "reason_code": ACK_POSITION_UNKNOWN,
                "valid": [], "invalidated": [], "closed": []}
    escopo = current_account_scope()
    if not escopo:
        # Conta/credencial trocada invalida TODAS as autorizações anteriores.
        alterados = await _end_acks([a["id"] for a in registro["acks"]],
                                    state_final="INVALIDATED",
                                    reason="conta/credencial não comprovada")
        return {"ok": False, "reason_code": ACK_NO_ACCOUNT, "valid": [],
                "invalidated": alterados, "closed": []}
    validos, invalidados, fechados = [], [], []
    for ack in registro["acks"]:
        chave = symbol_key(ack.get("symbol"))
        if str(ack.get("account_scope") or "") != escopo:
            invalidados.append((ack["id"], "conta divergente"))
            continue
        perna = _leg_for_symbol(positions, chave)
        if not perna["ok"]:
            if perna["reason_code"] == ACK_POSITION_ABSENT:
                fechados.append((ack["id"], "posição ausente em leitura fresca"))
            else:
                invalidados.append((ack["id"], "pernas ambíguas no símbolo"))
            continue
        posicao = perna["position"]
        atual = position_fingerprint(
            account_scope=escopo, exchange=EXCHANGE_BINANCE, market=MARKET_USDM_FUTURES,
            symbol=posicao["symbol"], side=posicao["side"],
            position_side=posicao["position_side"], qty=posicao["qty"],
            entry_price=posicao["entry_price"],
            update_time_ms=posicao["update_time_ms"],
            contract_version=ack.get("contract_version") or _contract_version())
        if atual is not None and atual == ack.get("fingerprint"):
            validos.append(ack["id"])
        else:
            invalidados.append((ack["id"], "identidade divergente da reconhecida"))
    mudou_inv = await _end_acks([i for i, _ in invalidados], state_final="INVALIDATED",
                                reasons=dict(invalidados))
    mudou_fech = await _end_acks([i for i, _ in fechados], state_final="CLOSED",
                                 reasons=dict(fechados))
    return {"ok": True, "reason_code": "REVALIDATED", "valid": validos,
            "invalidated": mudou_inv, "closed": mudou_fech}


async def _end_acks(ids: List[int], *, state_final: str,
                    reason: Optional[str] = None,
                    reasons: Optional[dict] = None) -> List[int]:
    """Encerra reconhecimentos preservando histórico (nunca apaga a linha)."""
    if not ids:
        return []
    try:
        from db import get_session
        from models.manual_position_ack import STATE_ACTIVE
        from models.manual_position_ack import ManualPositionAcknowledgement as Ack
        from sqlalchemy import select
        agora = _now()
        alterados = []
        async with get_session() as session:
            async with session.begin():
                linhas = (await session.execute(
                    select(Ack).where(Ack.id.in_(list(ids)), Ack.state == STATE_ACTIVE)
                    .with_for_update())).scalars().all()
                for linha in linhas:
                    linha.state = state_final
                    linha.ended_at = agora
                    linha.updated_at = agora
                    linha.ended_reason = _short(
                        (reasons or {}).get(linha.id) or reason or state_final, 120)
                    alterados.append(linha.id)
        return alterados
    except Exception as exc:  # noqa: BLE001
        log.error(f"[manual-ack] encerramento falhou: {type(exc).__name__}: {exc}")
        return []


async def ack_for_symbol(symbol: Any) -> Dict[str, Any]:
    """Reconhecimento ACTIVE daquele símbolo (ou None). Erro ⇒ `ok=False`."""
    registro = await active_acknowledgements()
    if not registro["ok"]:
        return {"ok": False, "reason_code": registro["reason_code"], "ack": None}
    chave = symbol_key(symbol)
    for ack in registro["acks"]:
        if symbol_key(ack.get("symbol")) == chave:
            return {"ok": True, "reason_code": "ACK_FOUND", "ack": ack}
    return {"ok": True, "reason_code": "ACK_ABSENT", "ack": None}
