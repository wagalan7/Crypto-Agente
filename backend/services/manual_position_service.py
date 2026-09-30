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
#: A prova de validação do reconhecimento venceu (ou nunca existiu). NOVA
#: exposição exige prova fresca; manutenção protetiva de posição BOT não exige.
GUARD_PROOF_STALE = "MANUAL_ACK_PROOF_STALE"
GUARD_AMBIGUOUS_REGISTRY = "MANUAL_ACK_REGISTRY_AMBIGUOUS"

#: Estados do reconhecimento que BLOQUEIAM o símbolo. Importados do modelo para
#: que transporte, admissão, reconciliador e índice usem o MESMO predicado.
try:  # pragma: no cover - fallback só para import parcial em ferramentas
    from models.manual_position_ack import (BLOCKING_STATES, ENDED_STATES,
                                            STATE_ACTIVE, STATE_CLOSED,
                                            STATE_INVALIDATED, STATE_SUPERSEDED,
                                            STATE_WAITING_ORDERS)
except Exception:  # noqa: BLE001
    STATE_ACTIVE, STATE_INVALIDATED = "ACTIVE", "INVALIDATED"
    STATE_WAITING_ORDERS, STATE_CLOSED = "WAITING_ORDERS", "CLOSED"
    STATE_SUPERSEDED = "SUPERSEDED"
    BLOCKING_STATES = (STATE_ACTIVE, STATE_INVALIDATED, STATE_WAITING_ORDERS)
    ENDED_STATES = (STATE_CLOSED, STATE_SUPERSEDED)

#: Idade MÁXIMA (s) da PROVA DE VALIDAÇÃO de um reconhecimento para autorizar
#: NOVA exposição. O ciclo oficial renova essa prova; vencida, nega e deixa o
#: ciclo atualizar — sem HTTP recursivo dentro do transporte.
VALIDATION_MAX_AGE_S = 900.0

#: Escopos possíveis de uma OBSERVAÇÃO de posições.
SCOPE_ACCOUNT = "ACCOUNT"
SCOPE_SYMBOL = "SYMBOL"

#: Ações que NÃO são nova exposição: manutenção protetiva/redutora de uma
#: posição BOT já existente. Elas continuam exigindo ownership do símbolo, mas
#: não exigem prova de validação fresca — travá-las deixaria posição BOT
#: comprovada sem stop por causa de um reconhecimento de OUTRO símbolo.
PROTECTIVE_ACTIONS = ("place_protection_orders", "cancel_order",
                      "cancel_algo_order", "trade_manager", "emergency_close",
                      "reduce_only")

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


def _observation(*, ok: bool, reason_code: str, positions: List[dict],
                 scope: str, symbol: Optional[str], complete: bool,
                 started_ms: Optional[int] = None, ended_ms: Optional[int] = None,
                 account_scope: Optional[str] = None,
                 detail: Optional[str] = None) -> Dict[str, Any]:
    """Contrato EXPLÍCITO de uma observação de posições.

    Lista vazia só prova ausência quando a resposta é COMPLETA e FRESCA para o
    alvo declarado. Uma observação `SYMBOL=ALFA` não diz nada sobre BETA.
    """
    fim = ended_ms if ended_ms is not None else _now_ms()
    return {"ok": bool(ok), "reason_code": reason_code,
            "positions": list(positions), "scope": scope,
            "symbol": (canonical_symbol(symbol) if symbol else None),
            "symbol_key": (symbol_key(symbol) if symbol else None),
            "complete": bool(complete and ok),
            "observed_start_ms": started_ms if started_ms is not None else fim,
            "observed_end_ms": fim, "observed_at_ms": fim,
            "account_scope": account_scope, "exchange": EXCHANGE_BINANCE,
            "market": MARKET_USDM_FUTURES, "detail": detail}


async def observe_positions(symbol: Optional[str] = None) -> Dict[str, Any]:
    """Leitura FRESCA das posições, com escopo e completude EXPLÍCITOS.

    Stale, rate-limited, erro, fonte não configurada, formato desconhecido ou
    linha com campo essencial inválido ⇒ `ok=False`/`complete=False`: fonte
    incerta NUNCA autoriza reconhecimento nem liberação, e nenhuma linha
    inválida é descartada em silêncio para depois concluir flat.
    """
    escopo = SCOPE_SYMBOL if symbol else SCOPE_ACCOUNT
    conta = current_account_scope()
    inicio = _now_ms()

    def falha(reason_code, detalhe):
        return _observation(ok=False, reason_code=reason_code, positions=[],
                            scope=escopo, symbol=symbol, complete=False,
                            started_ms=inicio, account_scope=conta,
                            detail=detalhe)

    try:
        from services import binance_signed_service as bss
    except Exception as exc:  # noqa: BLE001
        return falha(ACK_POSITION_UNKNOWN, type(exc).__name__)
    try:
        if not bss.is_configured():
            return falha(ACK_POSITION_UNKNOWN, "exchange não configurada")
        res = await bss.get_positions(symbol, force=True)
    except Exception as exc:  # noqa: BLE001
        return falha(ACK_POSITION_UNKNOWN, type(exc).__name__)
    if not isinstance(res, dict) or not res.get("ok") or res.get("stale") \
            or res.get("rate_limited"):
        return falha(ACK_POSITION_UNKNOWN, "leitura stale/rate-limited/indisponível")
    linhas, completo = normalize_positions(res.get("positions"))
    if not completo:
        return falha(ACK_POSITION_UNKNOWN, "linha de posição malformada na resposta")
    if escopo == SCOPE_SYMBOL:
        alvo = symbol_key(symbol)
        linhas = [p for p in linhas if p.get("symbol_key") == alvo]
    return _observation(ok=True, reason_code="POSITIONS_FRESH", positions=linhas,
                        scope=escopo, symbol=symbol, complete=True,
                        started_ms=inicio, account_scope=conta)


def normalize_positions(raw: Any) -> tuple:
    """`(linhas, completo)` a partir de linhas JÁ lidas de `get_positions`.

    Reaproveitado pelo reconciliador, que já fez a leitura fresca do ciclo. Uma
    linha em formato desconhecido ou com qty inválida marca a normalização como
    INCOMPLETA — descartá-la em silêncio permitiria concluir "flat" a partir de
    uma resposta que não foi entendida.
    """
    linhas, completo = [], True
    if raw is None:
        return linhas, completo
    if not isinstance(raw, (list, tuple)):
        return linhas, False
    for bruta in raw:
        if not isinstance(bruta, Mapping):
            completo = False
            continue
        quantidade = canonical_decimal(bruta.get("size"))
        if quantidade is None:
            completo = False
            continue
        if quantidade <= 0:
            continue                      # posição zerada não é linha inválida
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
    return linhas, completo


def observation_from_rows(raw: Any, *, symbol: Optional[str] = None,
                          source_ok: bool = True,
                          started_ms: Optional[int] = None) -> Dict[str, Any]:
    """Observação a partir de linhas JÁ lidas no ciclo (sem nova chamada HTTP)."""
    escopo = SCOPE_SYMBOL if symbol else SCOPE_ACCOUNT
    linhas, completo = normalize_positions(raw)
    if not source_ok or not completo:
        return _observation(ok=False, reason_code=ACK_POSITION_UNKNOWN,
                            positions=[], scope=escopo, symbol=symbol,
                            complete=False, started_ms=started_ms,
                            account_scope=current_account_scope(),
                            detail="leitura incompleta ou malformada")
    return _observation(ok=True, reason_code="POSITIONS_FRESH", positions=linhas,
                        scope=escopo, symbol=symbol, complete=True,
                        started_ms=started_ms,
                        account_scope=current_account_scope())


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
    """Reconhecimentos em estado BLOQUEANTE (ACTIVE/INVALIDATED/WAITING_ORDERS).

    "Não está ACTIVE" NÃO significa "símbolo livre": só `CLOSED` (flat provado
    + ausência fresca de ordens) e `SUPERSEDED` (substituído por confirmação
    nova) deixam de bloquear. Erro de leitura devolve `ok=False` — registro
    inacessível não vira lista vazia nem exceção solta no chamador.
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
        from sqlalchemy import select
        async with get_session() as session:
            linhas = (await session.execute(
                select(Ack).where(Ack.state.in_(list(BLOCKING_STATES))))).scalars().all()
            return {"ok": True, "reason_code": "REGISTRY_READ",
                    "acks": [linha.to_public() for linha in linhas]}
    except Exception as exc:  # noqa: BLE001
        log.error(f"[manual-ack] registro inacessível: {type(exc).__name__}: {exc}")
        return {"ok": False, "reason_code": GUARD_REGISTRY_UNAVAILABLE,
                "detail": type(exc).__name__, "acks": []}


def validation_proof_age_s(ack: Mapping) -> Optional[float]:
    """Idade (s) da prova de validação. `None` quando não há prova."""
    carimbo = (ack or {}).get("validated_at_ms")
    if carimbo in (None, "") or isinstance(carimbo, bool):
        return None
    try:
        valor = int(carimbo)
    except (TypeError, ValueError):
        return None
    if valor <= 0:
        return None
    return max(0.0, (_now_ms() - valor) / 1000.0)


def _proof_is_fresh(ack: Mapping) -> bool:
    idade = validation_proof_age_s(ack)
    return idade is not None and idade <= VALIDATION_MAX_AGE_S


async def ownership_guard(symbol: Any = None, *, action: str = "mutate",
                          require_fresh_proof: bool = False) -> Dict[str, Any]:
    """GUARD ÚNICO: o bot pode tocar neste símbolo/conta?

    Contrato fail-closed:

    - registro ilegível ⇒ BLOQUEIA (não sabemos se o símbolo é manual);
    - símbolo em estado BLOQUEANTE ⇒ BLOQUEIA (inclusive direção contrária e
      hedge — neste pacote o símbolo inteiro fica indisponível);
    - símbolo DESCONHECIDO pelo chamador, havendo registro bloqueante
      ⇒ BLOQUEIA (prova inconclusiva não autoriza mutação);
    - `require_fresh_proof` (NOVA exposição): algum registro bloqueante sem
      prova de validação fresca ⇒ BLOQUEIA. Manutenção protetiva de posição BOT
      em OUTRO símbolo não exige essa prova, senão um reconhecimento alheio
      deixaria a posição do bot sem stop;
    - sem registro bloqueante ⇒ LIBERA (comportamento anterior preservado).

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
    do_simbolo = [a for a in acks if symbol_key(a.get("symbol")) == chave]
    if len(do_simbolo) > 1:
        # Legado ambíguo: NÃO se escolhe uma linha. Fail-closed e visível.
        return {"allowed": False, "reason_code": GUARD_AMBIGUOUS_REGISTRY,
                "action": action, "symbol": canonical_symbol(symbol),
                "detail": f"{len(do_simbolo)} registros bloqueantes no símbolo"}
    if do_simbolo:
        ack = do_simbolo[0]
        return {"allowed": False, "reason_code": GUARD_MANUAL_SYMBOL,
                "action": action, "symbol": ack.get("symbol"),
                "ack_id": ack.get("id"), "side": ack.get("side"),
                "ack_state": ack.get("state"),
                "detail": "símbolo com posição manual reconhecida"}
    if require_fresh_proof and str(action) not in PROTECTIVE_ACTIONS:
        vencidos = [a for a in acks if not _proof_is_fresh(a)]
        if vencidos:
            return {"allowed": False, "reason_code": GUARD_PROOF_STALE,
                    "action": action, "symbol": canonical_symbol(symbol),
                    "ack_ids": [a.get("id") for a in vencidos],
                    "detail": ("reconhecimento manual sem validação fresca — "
                               "o ciclo oficial precisa revalidar antes de nova "
                               "exposição")}
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
    """Há ordens VIVAS naquele símbolo? DUAS fontes reais; incerteza ⇒ `ok=False`.

    Fontes: ordens COMUNS abertas (`openOrders`) e ALGO/condicionais
    (`openAlgoOrders`). O banco do bot não conhece as LIMIT abertas pelo
    operador, então provar ausência exige a corretora.

    Erro em QUALQUER fonte, resposta incompleta ou formato desconhecido =
    ausência NÃO comprovada. Nenhuma ordem é cancelada para alcançar ausência.
    """
    chave = symbol_key(symbol)
    canonico = canonical_symbol(chave)
    try:
        from services import binance_signed_service as bss
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason_code": ACK_ORDERS_UNKNOWN,
                "detail": type(exc).__name__}
    fontes = {"algo": getattr(bss, "get_open_algo_orders", None),
              "common": getattr(bss, "get_open_orders", None)}
    total, detalhes = 0, {}
    for nome, leitor in fontes.items():
        if leitor is None:
            # Capability ausente é INCERTEZA: o símbolo continua bloqueado.
            return {"ok": False, "reason_code": ACK_ORDERS_UNKNOWN,
                    "detail": f"fonte de ordens ausente: {nome}"}
        try:
            res = await leitor(canonico)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason_code": ACK_ORDERS_UNKNOWN,
                    "detail": f"{nome}: {type(exc).__name__}"}
        if not isinstance(res, dict) or not res.get("ok"):
            return {"ok": False, "reason_code": ACK_ORDERS_UNKNOWN,
                    "detail": f"{nome}: listagem indisponível"}
        linhas = res.get("orders")
        if not isinstance(linhas, (list, tuple)):
            return {"ok": False, "reason_code": ACK_ORDERS_UNKNOWN,
                    "detail": f"{nome}: formato desconhecido"}
        vivas = 0
        for ordem in linhas:
            if not isinstance(ordem, Mapping):
                return {"ok": False, "reason_code": ACK_ORDERS_UNKNOWN,
                        "detail": f"{nome}: linha em formato desconhecido"}
            if symbol_key(ordem.get("symbol")) == chave:
                vivas += 1
        detalhes[nome] = vivas
        total += vivas
    return {"ok": True, "reason_code": "ORDERS_READ", "live": total > 0,
            "count": total, "by_source": detalhes}


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
    from models.manual_position_ack import (ACK_CONTRACT_VERSION,
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
                # TODOS os registros BLOQUEANTES daquela identidade, não só os
                # ACTIVE: um INVALIDATED/WAITING_ORDERS continua bloqueando e
                # precisa ser substituído explicitamente.
                bloqueantes = (await session.execute(
                    select(Ack).where(Ack.account_scope == account_scope,
                                      Ack.exchange == EXCHANGE_BINANCE,
                                      Ack.market == MARKET_USDM_FUTURES,
                                      Ack.symbol == canonical_symbol(chave),
                                      Ack.state.in_(list(BLOCKING_STATES)))
                    .with_for_update())).scalars().all()
                if len(bloqueantes) > 1:
                    # Legado ambíguo: NÃO se escolhe uma linha. Fail-closed.
                    raise _AckRefused(GUARD_AMBIGUOUS_REGISTRY,
                                      {"blocking_rows": len(bloqueantes)})
                anterior = bloqueantes[0] if bloqueantes else None
                if anterior is not None:
                    if (anterior.state == STATE_ACTIVE
                            and anterior.fingerprint == fingerprint):
                        # Repetir a MESMA confirmação é idempotente.
                        return {"ok": True, "acknowledged": True, "idempotent": True,
                                "reason_code": ACK_IDEMPOTENT,
                                "acknowledgement": anterior.to_public(),
                                "note": _POST_NOTE}
                    if anterior.state == STATE_ACTIVE:
                        # Registro ATIVO com identidade diferente: substituir
                        # exigiria invalidar antes. Recusa explícita.
                        raise _AckRefused(ACK_CONFLICTING_ACTIVE,
                                          {"active_fingerprint": anterior.fingerprint})
                    # INVALIDATED/WAITING_ORDERS: esta confirmação NOVA (com
                    # fingerprint atual e `confirm` literal) substitui o anterior
                    # como SUPERSEDED — histórico preservado, sem duas linhas
                    # bloqueantes e sem autoack.
                    anterior.state = STATE_SUPERSEDED
                    anterior.revision = int(anterior.revision or 0) + 1
                    anterior.ended_at = agora
                    anterior.updated_at = agora
                    anterior.ended_reason = _short(
                        "substituído por nova confirmação administrativa", 120)
                    await session.flush()
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
                              "superseded_ack_id": (anterior.id if anterior is not None
                                                    else None),
                              "untracked_incidents": [l["incident_key"] for l in untracked]},
                    created_at=agora, updated_at=agora, ended_at=None,
                    ended_reason=None, revision=1,
                    # A própria confirmação é uma validação fresca do símbolo.
                    validated_at_ms=int(observed_at_ms),
                    validation_scope=SCOPE_SYMBOL,
                    validation_account=account_scope)
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
async def revalidate_active(*, observation: Optional[Dict[str, Any]] = None,
                            positions: Optional[List[dict]] = None,
                            observed_ok: Optional[bool] = None) -> Dict[str, Any]:
    """Confere os reconhecimentos BLOQUEANTES contra uma OBSERVAÇÃO explícita.

    A observação declara escopo (`ACCOUNT`/`SYMBOL`), completude e instante.
    Regras:

    - observação `SYMBOL=X` só pode alterar X. Percorrer todos os registros
      exige observação `ACCOUNT` completa — uma consulta filtrada NUNCA prova
      ausência de um símbolo que não foi consultado;
    - observação incompleta/stale/malformada ⇒ `ok=False`, nada muda;
    - identidade idêntica ⇒ permanece ACTIVE e ganha prova de validação;
    - identidade divergente ⇒ `INVALIDATED` — o símbolo CONTINUA bloqueado até
      nova confirmação administrativa ou encerramento comprovado;
    - posição ausente ⇒ só vira `CLOSED` com ausência FRESCA de ordens comuns
      E condicionais; qualquer incerteza vira `WAITING_ORDERS` (bloqueado);
    - atualização sob a advisory lock `917283` com CAS de revisão: um scan
      atrasado não fecha um reconhecimento mais novo.
    """
    if observation is None:
        # Compatibilidade com chamadores antigos: lista nua é tratada como
        # observação de CONTA e sua completude tem de ser declarada.
        if positions is None:
            observation = await observe_positions()
        else:
            observation = observation_from_rows(
                positions, source_ok=(observed_ok is not False))
    if not isinstance(observation, Mapping):
        return {"ok": False, "reason_code": ACK_POSITION_UNKNOWN, "valid": [],
                "invalidated": [], "closed": [], "waiting": []}
    if not observation.get("ok") or not observation.get("complete"):
        return {"ok": False, "reason_code": observation.get("reason_code")
                or ACK_POSITION_UNKNOWN, "valid": [], "invalidated": [],
                "closed": [], "waiting": []}
    idade_s = max(0.0, (_now_ms() - int(observation.get("observed_end_ms")
                                        or _now_ms())) / 1000.0)
    if idade_s > ACK_MAX_READ_AGE_S:
        return {"ok": False, "reason_code": ACK_READ_TOO_OLD, "valid": [],
                "invalidated": [], "closed": [], "waiting": []}
    registro = await active_acknowledgements()
    if not registro["ok"]:
        return {"ok": False, "reason_code": registro["reason_code"],
                "valid": [], "invalidated": [], "closed": [], "waiting": []}
    if not registro["acks"]:
        return {"ok": True, "reason_code": "NO_BLOCKING_ACKS", "valid": [],
                "invalidated": [], "closed": [], "waiting": []}
    escopo = current_account_scope()
    if not escopo:
        # Conta/credencial não comprovada: nenhuma autorização anterior vale.
        resultado = await _transition_acks(
            [(a["id"], a.get("revision"), STATE_INVALIDATED,
              "conta/credencial não comprovada") for a in registro["acks"]])
        return {"ok": False, "reason_code": ACK_NO_ACCOUNT, "valid": [],
                "invalidated": resultado["changed"], "closed": [], "waiting": [],
                "persisted": resultado["ok"]}
    # A chave do alvo é derivada quando o chamador só declarou o símbolo.
    alvo = observation.get("symbol_key") or symbol_key(observation.get("symbol"))
    escopo_obs = str(observation.get("scope") or SCOPE_ACCOUNT)
    if escopo_obs == SCOPE_SYMBOL and not alvo:
        # Escopo SYMBOL sem alvo identificável não revalida nada.
        return {"ok": False, "reason_code": ACK_POSITION_UNKNOWN, "valid": [],
                "invalidated": [], "closed": [], "waiting": []}
    do_escopo = [a for a in registro["acks"]
                 if escopo_obs == SCOPE_ACCOUNT
                 or symbol_key(a.get("symbol")) == alvo]
    fora_do_escopo = [a["id"] for a in registro["acks"] if a not in do_escopo]
    posicoes = observation.get("positions") or []
    validos, transicoes = [], []
    for ack in do_escopo:
        chave = symbol_key(ack.get("symbol"))
        if str(ack.get("account_scope") or "") != escopo:
            transicoes.append((ack["id"], ack.get("revision"), STATE_INVALIDATED,
                               "conta divergente"))
            continue
        perna = _leg_for_symbol(posicoes, chave)
        if not perna["ok"]:
            if perna["reason_code"] != ACK_POSITION_ABSENT:
                transicoes.append((ack["id"], ack.get("revision"),
                                   STATE_INVALIDATED, "pernas ambíguas no símbolo"))
                continue
            # Flat COMPROVADO. Encerrar exige ausência fresca de ordens; nada é
            # cancelado para conseguir isso.
            ordens = await symbol_has_live_orders(ack.get("symbol"))
            if not ordens.get("ok"):
                transicoes.append((ack["id"], ack.get("revision"),
                                   STATE_WAITING_ORDERS,
                                   "ordens do símbolo não confirmadas"))
            elif ordens.get("live"):
                transicoes.append((ack["id"], ack.get("revision"),
                                   STATE_WAITING_ORDERS,
                                   f"{ordens.get('count')} ordem(ns) do operador viva(s)"))
            else:
                transicoes.append((ack["id"], ack.get("revision"), STATE_CLOSED,
                                   "posição e ordens ausentes em leitura fresca"))
            continue
        posicao = perna["position"]
        atual = position_fingerprint(
            account_scope=escopo, exchange=EXCHANGE_BINANCE, market=MARKET_USDM_FUTURES,
            symbol=posicao["symbol"], side=posicao["side"],
            position_side=posicao["position_side"], qty=posicao["qty"],
            entry_price=posicao["entry_price"],
            update_time_ms=posicao["update_time_ms"],
            contract_version=ack.get("contract_version") or _contract_version())
        if atual is not None and atual == ack.get("fingerprint") \
                and ack.get("state") == STATE_ACTIVE:
            validos.append(ack["id"])
        elif atual is not None and atual == ack.get("fingerprint"):
            # Compatível, mas o registro já não está ACTIVE (INVALIDATED/
            # WAITING_ORDERS): só uma NOVA confirmação administrativa reativa.
            validos.append(ack["id"])
        else:
            transicoes.append((ack["id"], ack.get("revision"), STATE_INVALIDATED,
                               "identidade divergente da reconhecida"))
    aplicadas = await _transition_acks(transicoes)
    prova = await record_validation_proof(
        validos, account_scope=escopo, scope=str(observation.get("scope")),
        validated_at_ms=int(observation.get("observed_end_ms") or _now_ms()))
    persistiu = aplicadas["ok"] and prova.get("ok", True)
    return {"ok": persistiu, "reason_code": ("REVALIDATED" if persistiu
                                             else "MANUAL_ACK_PERSISTENCE_FAILED"),
            "valid": validos,
            "invalidated": [i for i, s in aplicadas["by_state"] if s == STATE_INVALIDATED],
            "closed": [i for i, s in aplicadas["by_state"] if s == STATE_CLOSED],
            "waiting": [i for i, s in aplicadas["by_state"] if s == STATE_WAITING_ORDERS],
            "out_of_scope": fora_do_escopo, "scope": observation.get("scope"),
            "persisted": persistiu}


async def _transition_acks(transicoes: List[tuple]) -> Dict[str, Any]:
    """Aplica transições com CAS de revisão, sob a advisory lock `917283`.

    `transicoes` = [(id, revisao_lida, estado_final, motivo)]. A linha só muda
    se a revisão no banco ainda for a lida: um scan atrasado não sobrescreve um
    reconhecimento mais novo. Falha de persistência devolve `ok=False` — nunca
    lista vazia com `ok=True`.
    """
    if not transicoes:
        return {"ok": True, "changed": [], "by_state": [], "stale": []}
    try:
        from db import get_session
        from models.manual_position_ack import ManualPositionAcknowledgement as Ack
        from services.entry_intent_service import RISK_LOCK_KEY
        from sqlalchemy import select, text
        agora = _now()
        alterados, por_estado, obsoletos = [], [], []
        async with get_session() as session:
            async with session.begin():
                await session.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                                      {"k": RISK_LOCK_KEY})
                ids = [int(i) for i, _r, _s, _m in transicoes]
                linhas = {linha.id: linha for linha in (await session.execute(
                    select(Ack).where(Ack.id.in_(ids))
                    .with_for_update())).scalars().all()}
                for identificador, revisao, estado_final, motivo in transicoes:
                    linha = linhas.get(int(identificador))
                    if linha is None:
                        obsoletos.append(int(identificador))
                        continue
                    if revisao is not None and int(linha.revision or 0) != int(revisao):
                        obsoletos.append(int(identificador))   # CAS perdido
                        continue
                    if linha.state in ENDED_STATES:
                        obsoletos.append(int(identificador))
                        continue
                    linha.state = estado_final
                    linha.revision = int(linha.revision or 0) + 1
                    linha.updated_at = agora
                    linha.ended_reason = _short(motivo, 120)
                    linha.ended_at = agora if estado_final in ENDED_STATES else None
                    alterados.append(linha.id)
                    por_estado.append((linha.id, estado_final))
        return {"ok": True, "changed": alterados, "by_state": por_estado,
                "stale": obsoletos}
    except Exception as exc:  # noqa: BLE001
        log.error(f"[manual-ack] transição falhou: {type(exc).__name__}: {exc}")
        return {"ok": False, "changed": [], "by_state": [], "stale": [],
                "detail": type(exc).__name__}


async def record_validation_proof(ids: List[int], *, account_scope: str,
                                  scope: str, validated_at_ms: int) -> Dict[str, Any]:
    """Grava a PROVA de validação dos reconhecimentos confirmados agora.

    É essa prova que o guard consulta antes de autorizar NOVA exposição — ele
    apenas LÊ e compara, sem HTTP recursivo. Falha devolve `ok=False`.
    """
    if not ids:
        return {"ok": True, "updated": []}
    try:
        from db import get_session
        from models.manual_position_ack import ManualPositionAcknowledgement as Ack
        from sqlalchemy import select
        agora = _now()
        atualizados = []
        async with get_session() as session:
            async with session.begin():
                linhas = (await session.execute(
                    select(Ack).where(Ack.id.in_([int(i) for i in ids]))
                    .with_for_update())).scalars().all()
                for linha in linhas:
                    if linha.state in ENDED_STATES:
                        continue
                    linha.validated_at_ms = int(validated_at_ms)
                    linha.validation_scope = str(scope)[:16]
                    linha.validation_account = str(account_scope)[:64]
                    linha.updated_at = agora
                    atualizados.append(linha.id)
        return {"ok": True, "updated": atualizados}
    except Exception as exc:  # noqa: BLE001
        log.error(f"[manual-ack] prova de validação falhou: "
                  f"{type(exc).__name__}: {exc}")
        return {"ok": False, "updated": [], "detail": type(exc).__name__}


async def _end_acks(ids: List[int], *, state_final: str,
                    reason: Optional[str] = None,
                    reasons: Optional[dict] = None) -> Dict[str, Any]:
    """Compat: encerra reconhecimentos sem CAS (chamadores antigos).

    Devolve o MESMO contrato de `_transition_acks`: falha de persistência é
    `ok=False`, nunca lista vazia com sucesso.
    """
    return await _transition_acks([
        (int(i), None, state_final, (reasons or {}).get(i) or reason or state_final)
        for i in (ids or ())])


def ownership_from_rows(rows: Any, *, account_scope: Any, exchange: Any,
                        market: Any, symbol: Any, action: str = "mutate",
                        require_fresh_proof: bool = False) -> Dict[str, Any]:
    """Veredicto de ownership a partir de linhas JÁ lidas NA transação.

    PURO: não abre sessão, não chama exchange e não consulta cache de processo.
    É o núcleo de `check_ownership_in_session`.
    """
    chave = symbol_key(symbol)
    conta = str(account_scope or "").strip()
    if not conta or "/" not in chave:
        return {"allowed": False, "reason_code": GUARD_SYMBOL_UNKNOWN,
                "action": action,
                "detail": "conta/símbolo canônicos indisponíveis"}
    alvo = []
    for linha in (rows or ()):
        dados = linha if isinstance(linha, Mapping) else getattr(linha, "__dict__", {})
        if str(dados.get("account_scope") or "") != conta:
            continue
        if str(dados.get("exchange") or "").lower() != str(exchange or "").lower():
            continue
        if str(dados.get("market") or "").lower() != str(market or "").lower():
            continue
        if str(dados.get("state")) not in BLOCKING_STATES:
            continue
        if symbol_key(dados.get("symbol")) != chave:
            continue
        alvo.append(dados)
    if len(alvo) > 1:
        return {"allowed": False, "reason_code": GUARD_AMBIGUOUS_REGISTRY,
                "action": action, "symbol": canonical_symbol(chave),
                "detail": f"{len(alvo)} registros bloqueantes no símbolo"}
    if alvo:
        return {"allowed": False, "reason_code": GUARD_MANUAL_SYMBOL,
                "action": action, "symbol": canonical_symbol(chave),
                "ack_id": alvo[0].get("id"), "ack_state": alvo[0].get("state"),
                "detail": "símbolo com posição manual reconhecida"}
    if require_fresh_proof and str(action) not in PROTECTIVE_ACTIONS:
        vencidos = []
        for linha in (rows or ()):
            dados = linha if isinstance(linha, Mapping) else getattr(linha, "__dict__", {})
            if str(dados.get("state")) not in BLOCKING_STATES:
                continue
            if str(dados.get("account_scope") or "") != conta:
                continue
            if not _proof_is_fresh(dados):
                vencidos.append(dados.get("id"))
        if vencidos:
            return {"allowed": False, "reason_code": GUARD_PROOF_STALE,
                    "action": action, "symbol": canonical_symbol(chave),
                    "ack_ids": vencidos,
                    "detail": "reconhecimento manual sem validação fresca"}
    return {"allowed": True, "reason_code": GUARD_OK, "action": action,
            "symbol": canonical_symbol(chave)}


async def check_ownership_in_session(session, *, account_scope: Any,
                                     exchange: Any, market: Any, symbol: Any,
                                     action: str = "reserve",
                                     require_fresh_proof: bool = True
                                     ) -> Dict[str, Any]:
    """Ownership DENTRO da transação da admissão (só banco).

    Roda DEPOIS de adquirir a lock `917283` e ANTES de conceder/gravar
    capacidade. Não abre sessão própria, não chama exchange e não confia em
    cache de processo. Registro/prova indisponível = NEGAÇÃO.
    """
    try:
        from models.manual_position_ack import ManualPositionAcknowledgement as Ack
        from sqlalchemy import select
        linhas = (await session.execute(
            select(Ack).where(Ack.state.in_(list(BLOCKING_STATES))))).scalars().all()
    except Exception as exc:  # noqa: BLE001
        log.error(f"[manual-ack] ownership na transação falhou: "
                  f"{type(exc).__name__}: {exc}")
        return {"allowed": False, "reason_code": GUARD_REGISTRY_UNAVAILABLE,
                "action": action, "detail": type(exc).__name__}
    return ownership_from_rows(
        [linha.to_public() for linha in linhas], account_scope=account_scope,
        exchange=exchange, market=market, symbol=symbol, action=action,
        require_fresh_proof=require_fresh_proof)


async def ack_for_symbol(symbol: Any) -> Dict[str, Any]:
    """Reconhecimento BLOQUEANTE daquele símbolo (ou None). Erro ⇒ `ok=False`."""
    registro = await active_acknowledgements()
    if not registro["ok"]:
        return {"ok": False, "reason_code": registro["reason_code"], "ack": None}
    chave = symbol_key(symbol)
    encontrados = [a for a in registro["acks"]
                   if symbol_key(a.get("symbol")) == chave]
    if len(encontrados) > 1:
        return {"ok": False, "reason_code": GUARD_AMBIGUOUS_REGISTRY, "ack": None}
    if encontrados:
        return {"ok": True, "reason_code": "ACK_FOUND", "ack": encontrados[0]}
    return {"ok": True, "reason_code": "ACK_ABSENT", "ack": None}
