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


async def ownership_guard(symbol: Any = None, *, action: str = "mutate",
                          require_fresh_proof: bool = False) -> Dict[str, Any]:
    """GUARD ÚNICO: o bot pode tocar neste símbolo/conta?

    Contrato fail-closed:

    - registro ilegível ⇒ BLOQUEIA (não sabemos se o símbolo é manual);
    - símbolo em estado BLOQUEANTE ⇒ BLOQUEIA (inclusive direção contrária e
      hedge — neste pacote o símbolo inteiro fica indisponível);
    - símbolo DESCONHECIDO pelo chamador, havendo registro bloqueante
      ⇒ BLOQUEIA (prova inconclusiva não autoriza mutação);
    - `require_fresh_proof` (NOVA exposição): validação de conta BLOQUEADA, ou
      registro bloqueante sem prova COMPLETA (estado/revisão/conta/época/
      contrato/escopo/tempo), ⇒ BLOQUEIA. Manutenção protetiva/redutora de
      posição BOT em OUTRO símbolo não exige essa prova, senão um
      reconhecimento alheio deixaria a posição do bot sem stop;
    - sem registro bloqueante ⇒ LIBERA, exceto se a validação de conta estiver
      bloqueada e a ação for de NOVA exposição.

    NÃO consulta a exchange: é chamado de dentro de locks/semáforos do próprio
    transporte, onde I/O HTTP recursivo é proibido.
    """
    nova_exposicao = (require_fresh_proof
                      and str(action) not in PROTECTIVE_ACTIONS)
    estado_conta = None
    if nova_exposicao:
        estado_conta = await account_validation_state()
        if estado_conta.get("blocked"):
            return {"allowed": False, "reason_code": GUARD_ACCOUNT_BLOCKED,
                    "action": action,
                    "symbol": (canonical_symbol(symbol) if symbol else None),
                    "detail": estado_conta.get("reason_code")}
    registro = await active_acknowledgements()
    if not registro["ok"]:
        return {"allowed": False, "reason_code": GUARD_REGISTRY_UNAVAILABLE,
                "action": action, "symbol": None,
                "detail": registro.get("detail")}
    # Só a conta VIGENTE importa: reconhecimento de outra conta/credencial não
    # bloqueia nem autoriza nada aqui.
    conta_atual = str(current_account_scope() or "")
    acks = [a for a in registro["acks"]
            if str(a.get("account_scope") or "") == conta_atual]
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
    if nova_exposicao:
        conta = conta_atual
        epoca = (estado_conta or {}).get("generation")
        vencidos = [a.get("id") for a in acks
                    if not proof_is_valid(a, account_scope=conta,
                                          manual_generation=epoca)]
        if vencidos:
            return {"allowed": False, "reason_code": GUARD_PROOF_STALE,
                    "action": action, "symbol": canonical_symbol(symbol),
                    "ack_ids": vencidos,
                    "detail": ("reconhecimento manual sem validação completa — "
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
    from services.entry_intent_service import acquire_risk_lock, bump_manual_generation
    from sqlalchemy import select

    agora = _now()
    try:
        async with get_session() as session:
            async with session.begin():
                # MESMA lock da admissão: reconhecimento e reserva de entrada
                # são mutuamente exclusivos (ordem única de locks).
                await acquire_risk_lock(session)
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
                    ended_reason=None, revision=1)
                # Confirmação administrativa AVANÇA o fence manual e deixa a
                # conta aguardando o próximo ciclo COMPLETO (§7.1): ela valida
                # o SÍMBOLO, não a conta.
                epoca_manual = await bump_manual_generation(
                    session, account_scope=account_scope,
                    exchange=EXCHANGE_BINANCE, market=MARKET_USDM_FUTURES,
                    blocked=True)
                # A própria confirmação é uma validação fresca DESTE símbolo,
                # com todos os componentes da prova.
                registro.validated_at_ms = int(observed_at_ms)
                registro.validation_scope = SCOPE_SYMBOL
                registro.validation_account = account_scope
                registro.validated_revision = 1
                registro.validated_generation = int(epoca_manual)
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
# ════════════════════════════════════════════════════════════════════════════
#  Contexto de validação, prova com CAS, falha e recuperação
# ════════════════════════════════════════════════════════════════════════════
#  Regra central: a identidade é capturada ANTES da leitura externa (GET) e
#  conferida no commit. Um resultado que nasceu antes de uma mudança não pode
#  encerrar, invalidar ou validar o que mudou depois dele.
#
#  `STALE_CONTEXT` (perda de corrida) NÃO arma pausa, NÃO incrementa épocas e
#  NÃO revoga a prova do vencedor: descarta o resultado e pede novo ciclo. É
#  diferente de falha EXTERNA (stale/erro/incompleto/persistência), tratada em
#  `register_validation_failure`.
STALE_CONTEXT = "MANUAL_ACK_STALE_CONTEXT"
GUARD_ACCOUNT_BLOCKED = "MANUAL_ACCOUNT_VALIDATION_BLOCKED"
VALIDATION_FAILURE = "MANUAL_VALIDATION_FAILED"

#: Fence LOCAL monotônico de falha, por processo. Ele avança ANTES de tentar
#: persistir: se o commit falhar, o publicador ainda recusa contextos anteriores
#: à falha — não se afirma que uma falha sem commit já é conhecida por outro
#: processo.
_LOCAL_VALIDATION: Dict[str, Any] = {"fence": 0, "pending": None}


def reset_local_validation_state() -> None:
    """Simula processo NOVO (boot). Boot começa fechado: o estado durável manda."""
    _LOCAL_VALIDATION["fence"] = 0
    _LOCAL_VALIDATION["pending"] = None


def local_validation_fence() -> int:
    return int(_LOCAL_VALIDATION["fence"])


def _advance_local_fence(reason: str) -> int:
    _LOCAL_VALIDATION["fence"] = int(_LOCAL_VALIDATION["fence"]) + 1
    _LOCAL_VALIDATION["pending"] = {"reason": _short(reason, 120),
                                    "fence": _LOCAL_VALIDATION["fence"],
                                    "at_ms": _now_ms()}
    return _LOCAL_VALIDATION["fence"]


def pending_validation_failure() -> Optional[dict]:
    pendente = _LOCAL_VALIDATION.get("pending")
    return dict(pendente) if pendente else None


def proof_is_valid(ack: Mapping, *, account_scope: Any, manual_generation: Any,
                   now_ms: Optional[int] = None) -> bool:
    """A prova deste reconhecimento autoriza NOVA exposição agora?

    Idade sozinha NÃO é prova. Exige, em conjunto: estado ACTIVE,
    `revision == validated_revision`, conta de validação igual à atual, época da
    prova igual à época manual vigente, contrato/exchange/mercado corretos,
    escopo reconhecido e aplicável, carimbo inteiro positivo e NÃO futuro, e
    idade dentro do limite. Nada de `max(0, idade)` validando futuro.
    """
    dados = ack if isinstance(ack, Mapping) else {}
    if str(dados.get("state")) != STATE_ACTIVE:
        return False
    revisao, validada = dados.get("revision"), dados.get("validated_revision")
    if validada is None or isinstance(validada, bool):
        return False
    try:
        if int(revisao) != int(validada):
            return False
    except (TypeError, ValueError):
        return False
    conta = str(account_scope or "")
    if not conta or str(dados.get("validation_account") or "") != conta:
        return False
    epoca = dados.get("validated_generation")
    if epoca is None or isinstance(epoca, bool):
        return False
    try:
        if int(epoca) != int(manual_generation):
            return False
    except (TypeError, ValueError):
        return False
    if str(dados.get("contract_version") or "") != _contract_version():
        return False
    if dados.get("exchange") is not None \
            and str(dados.get("exchange")).lower() != EXCHANGE_BINANCE:
        return False
    if dados.get("market") is not None \
            and str(dados.get("market")).lower() != MARKET_USDM_FUTURES:
        return False
    escopo = str(dados.get("validation_scope") or "")
    if escopo not in (SCOPE_ACCOUNT, SCOPE_SYMBOL):
        return False
    if escopo == SCOPE_SYMBOL:
        # Prova de SÍMBOLO vale para o reconhecimento DAQUELE símbolo.
        alvo = dados.get("validation_symbol") or dados.get("symbol")
        if symbol_key(alvo) != symbol_key(dados.get("symbol")):
            return False
    carimbo = dados.get("validated_at_ms")
    if carimbo is None or isinstance(carimbo, bool):
        return False
    try:
        carimbo = int(carimbo)
    except (TypeError, ValueError):
        return False
    if carimbo <= 0:
        return False
    agora = int(now_ms if now_ms is not None else _now_ms())
    if carimbo > agora + _CLOCK_SKEW_MS:
        return False                      # futuro incoerente nunca é prova
    return (agora - carimbo) / 1000.0 <= VALIDATION_MAX_AGE_S


#: Tolerância de relógio ao comparar carimbos (ms).
_CLOCK_SKEW_MS = 2_000


def _proof_is_fresh(ack: Mapping) -> bool:
    """Compat: idade bruta. O predicado COMPLETO é `proof_is_valid`."""
    carimbo = (ack or {}).get("validated_at_ms")
    if carimbo in (None, "") or isinstance(carimbo, bool):
        return False
    try:
        valor = int(carimbo)
    except (TypeError, ValueError):
        return False
    agora = _now_ms()
    if valor <= 0 or valor > agora + _CLOCK_SKEW_MS:
        return False
    return (agora - valor) / 1000.0 <= VALIDATION_MAX_AGE_S


async def account_validation_state(account_scope: Any = None) -> Dict[str, Any]:
    """Estado DURÁVEL da validação manual daquela conta.

    Falha pendente LOCAL (commit que não aconteceu) também bloqueia: não se
    afirma que uma falha sem commit já é conhecida por outro processo.
    """
    conta = str(account_scope or current_account_scope() or "")
    pendente = pending_validation_failure()
    if not conta:
        # SEM conta comprovada não existe reconhecimento manual: o subsistema é
        # inerte. Não é liberação disfarçada — sem credencial o transporte
        # recusa (`is_configured`) e nenhuma intenção pode ser reservada
        # (`_entry_account_ref` devolve None).
        return {"blocked": bool(pendente), "generation": 0,
                "pending_failure": bool(pendente),
                "reason_code": ("MANUAL_VALIDATION_INERT" if not pendente
                                else GUARD_ACCOUNT_BLOCKED)}
    try:
        from db import DB_ENABLED, get_session
    except Exception as exc:  # noqa: BLE001
        return {"blocked": True, "generation": 0, "pending_failure": True,
                "reason_code": GUARD_REGISTRY_UNAVAILABLE, "detail": type(exc).__name__}
    if not DB_ENABLED:
        # Sem banco não existe reconhecimento nem bloqueio manual.
        return {"blocked": False, "generation": 0, "pending_failure": False,
                "reason_code": "REGISTRY_DISABLED"}
    try:
        from models.account_margin_epoch import AccountMarginEpoch as Epoch
        from sqlalchemy import select
        async with get_session() as session:
            linha = (await session.execute(
                select(Epoch).where(Epoch.account_scope == conta,
                                    Epoch.exchange == EXCHANGE_BINANCE,
                                    Epoch.market == MARKET_USDM_FUTURES))).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        return {"blocked": True, "generation": 0, "pending_failure": True,
                "reason_code": GUARD_REGISTRY_UNAVAILABLE, "detail": type(exc).__name__}
    if linha is None:
        # Conta sem linha nasce BLOQUEADA: só ciclo completo real libera.
        return {"blocked": True, "generation": 0, "pending_failure": bool(pendente),
                "reason_code": GUARD_ACCOUNT_BLOCKED}
    bloqueada = bool(linha.manual_validation_blocked) or bool(pendente)
    return {"blocked": bloqueada,
            "generation": int(linha.manual_validation_generation or 0),
            "pending_failure": bool(pendente),
            "reason_code": (GUARD_ACCOUNT_BLOCKED if bloqueada else "ACCOUNT_VALIDATED")}


async def capture_validation_context(*, scope: str = SCOPE_ACCOUNT,
                                     symbol: Any = None) -> Dict[str, Any]:
    """Captura a identidade ANTES da leitura externa, sob a lock `917283`.

    A transação fecha ANTES do GET: nenhuma lock é mantida durante rede.
    """
    conta = current_account_scope()
    base = {"ok": False, "scope": scope,
            "symbol": (canonical_symbol(symbol) if symbol else None),
            "symbol_key": (symbol_key(symbol) if symbol else None),
            "account_scope": conta, "exchange": EXCHANGE_BINANCE,
            "market": MARKET_USDM_FUTURES, "acks": [], "ack_ids": [],
            "manual_generation": None, "local_fence": local_validation_fence(),
            "started_at_ms": _now_ms(), "reason_code": None}
    if not conta:
        # Subsistema INERTE (sem credencial comprovada não há reconhecimento
        # daquela conta). Contexto válido e vazio: nada a validar ou encerrar.
        return {**base, "ok": True, "manual_generation": 0, "inert": True,
                "reason_code": "MANUAL_VALIDATION_INERT"}
    try:
        from db import DB_ENABLED, get_session
    except Exception as exc:  # noqa: BLE001
        return {**base, "reason_code": GUARD_REGISTRY_UNAVAILABLE,
                "detail": type(exc).__name__}
    if not DB_ENABLED:
        return {**base, "ok": True, "manual_generation": 0,
                "reason_code": "REGISTRY_DISABLED"}
    try:
        from models.manual_position_ack import ManualPositionAcknowledgement as Ack
        from services.entry_intent_service import (acquire_risk_lock,
                                                   current_manual_generation)
        from sqlalchemy import select
        async with get_session() as session:
            async with session.begin():
                await acquire_risk_lock(session)
                epoca = await current_manual_generation(
                    session, account_scope=conta, exchange=EXCHANGE_BINANCE,
                    market=MARKET_USDM_FUTURES)
                consulta = select(Ack).where(
                    Ack.account_scope == conta,
                    Ack.exchange == EXCHANGE_BINANCE,
                    Ack.market == MARKET_USDM_FUTURES,
                    Ack.state.in_(list(BLOCKING_STATES))).order_by(Ack.id)
                linhas = (await session.execute(consulta)).scalars().all()
        if scope == SCOPE_SYMBOL:
            alvo = symbol_key(symbol)
            linhas = [l for l in linhas if symbol_key(l.symbol) == alvo]
        instantaneo = [{"id": int(l.id), "revision": int(l.revision or 0),
                        "fingerprint": l.fingerprint, "state": l.state,
                        "symbol": l.symbol,
                        "contract_version": l.contract_version} for l in linhas]
        return {**base, "ok": True, "manual_generation": int(epoca),
                "acks": instantaneo, "ack_ids": [l["id"] for l in instantaneo],
                "reason_code": "CONTEXT_CAPTURED", "started_at_ms": _now_ms()}
    except Exception as exc:  # noqa: BLE001
        log.error(f"[manual-ack] captura de contexto falhou: {type(exc).__name__}: {exc}")
        return {**base, "reason_code": GUARD_REGISTRY_UNAVAILABLE,
                "detail": type(exc).__name__}


async def register_validation_failure(*, reason: str,
                                      context: Optional[Dict[str, Any]] = None
                                      ) -> Dict[str, Any]:
    """Falha EXTERNA conhecida: bloqueia IMEDIATAMENTE e tenta persistir.

    1. avança o fence LOCAL e registra a causa pendente ANTES de tentar gravar;
    2. sob `917283`, avança `manual_validation_generation`, marca `blocked` e
       revoga as provas alcançadas;
    3. erro de banco mantém latch/causa pendente e devolve UNKNOWN — o commit
       pode NÃO ter acontecido.
    """
    _advance_local_fence(reason)
    resultado = await _persist_validation_failure(context=context, reason=reason)
    if resultado.get("ok"):
        _LOCAL_VALIDATION["pending"] = None
    return {"ok": bool(resultado.get("ok")), "reason_code": VALIDATION_FAILURE,
            "detail": _short(reason, 120),
            "generation": resultado.get("generation"),
            "pending_failure": pending_validation_failure() is not None}


async def flush_pending_validation_failure() -> Dict[str, Any]:
    """Persiste a falha pendente quando o banco volta, ANTES de recuperar."""
    pendente = pending_validation_failure()
    if not pendente:
        return {"ok": True, "reason_code": "NO_PENDING_FAILURE"}
    resultado = await _persist_validation_failure(
        context=None, reason=str(pendente.get("reason") or VALIDATION_FAILURE))
    if resultado.get("ok"):
        _LOCAL_VALIDATION["pending"] = None
    return {"ok": bool(resultado.get("ok")), "reason_code": VALIDATION_FAILURE,
            "generation": resultado.get("generation")}


async def _persist_validation_failure(*, context: Optional[Dict[str, Any]],
                                      reason: str) -> Dict[str, Any]:
    """Avança a época manual, bloqueia a conta e revoga provas — UM commit."""
    conta = str((context or {}).get("account_scope")
                or current_account_scope() or "")
    if not conta:
        return {"ok": False, "reason_code": ACK_NO_ACCOUNT}
    try:
        from db import DB_ENABLED, get_session
        if not DB_ENABLED:
            return {"ok": True, "reason_code": "REGISTRY_DISABLED", "generation": 0}
        from models.manual_position_ack import ManualPositionAcknowledgement as Ack
        from services.entry_intent_service import (acquire_risk_lock,
                                                   bump_manual_generation)
        from sqlalchemy import select
        agora = _now()
        async with get_session() as session:
            async with session.begin():
                await acquire_risk_lock(session)
                nova = await bump_manual_generation(
                    session, account_scope=conta, exchange=EXCHANGE_BINANCE,
                    market=MARKET_USDM_FUTURES, blocked=True)
                linhas = (await session.execute(
                    select(Ack).where(Ack.account_scope == conta,
                                      Ack.exchange == EXCHANGE_BINANCE,
                                      Ack.market == MARKET_USDM_FUTURES,
                                      Ack.validated_at_ms.is_not(None))
                    .order_by(Ack.id).with_for_update())).scalars().all()
                for linha in linhas:
                    _revoke_proof(linha, agora)
        return {"ok": True, "reason_code": VALIDATION_FAILURE, "generation": int(nova)}
    except Exception as exc:  # noqa: BLE001
        log.error(f"[manual-ack] persistir falha de validação: "
                  f"{type(exc).__name__}: {exc}")
        return {"ok": False, "reason_code": GUARD_REGISTRY_UNAVAILABLE,
                "detail": type(exc).__name__}


def _revoke_proof(linha, agora) -> None:
    """Revoga a prova DENTRO da transação corrente (sem reabrir sessão)."""
    linha.validated_at_ms = None
    linha.validation_scope = None
    linha.validation_account = None
    linha.validated_revision = None
    linha.validated_generation = None
    linha.updated_at = agora


async def revalidate_active(*, observation: Optional[Dict[str, Any]] = None,
                            context: Optional[Dict[str, Any]] = None,
                            positions: Optional[List[dict]] = None,
                            observed_ok: Optional[bool] = None) -> Dict[str, Any]:
    """Revalida os reconhecimentos BLOQUEANTES com CONTEXTO anterior ao GET.

    Fluxo: contexto capturado sob lock → GET fresco fora do banco → decisão e
    commit único sob lock, conferindo revisão/estado/fingerprint de CADA linha e
    a época manual. Observação parcial/stale/incompleta é FALHA (bloqueia a
    conta); perda de corrida é `STALE_CONTEXT` (apenas descarta e pede ciclo).
    """
    if observation is None:
        if positions is None:
            if context is None:
                context = await capture_validation_context(scope=SCOPE_ACCOUNT)
            observation = await observe_positions()
        else:
            observation = observation_from_rows(
                positions, source_ok=(observed_ok is not False))
    if context is None:
        # Sem captura anterior à leitura não se publica prova nem se encerra
        # reconhecimento: agenda novo ciclo.
        return {"ok": False, "reason_code": STALE_CONTEXT, "valid": [],
                "invalidated": [], "closed": [], "waiting": [],
                "detail": "observação sem contexto anterior ao GET"}
    if not context.get("ok"):
        return {"ok": False, "reason_code": context.get("reason_code")
                or GUARD_REGISTRY_UNAVAILABLE, "valid": [], "invalidated": [],
                "closed": [], "waiting": []}
    if not isinstance(observation, Mapping) or not observation.get("ok") \
            or not observation.get("complete"):
        # Falha EXTERNA: bloqueia a conta imediatamente.
        falha = await register_validation_failure(
            reason=str((observation or {}).get("reason_code") or ACK_POSITION_UNKNOWN),
            context=context)
        return {"ok": False, "reason_code": (observation or {}).get("reason_code")
                or ACK_POSITION_UNKNOWN, "valid": [], "invalidated": [],
                "closed": [], "waiting": [], "failure_persisted": falha["ok"]}
    if int(context.get("local_fence") or 0) != local_validation_fence():
        # Houve falha conhecida DEPOIS da captura: este GET não repara nada.
        return {"ok": False, "reason_code": STALE_CONTEXT, "valid": [],
                "invalidated": [], "closed": [], "waiting": [],
                "detail": "fence local avançou após a captura"}
    if pending_validation_failure() is not None:
        return {"ok": False, "reason_code": VALIDATION_FAILURE, "valid": [],
                "invalidated": [], "closed": [], "waiting": [],
                "detail": "falha pendente precisa ser persistida antes"}
    idade_s = max(0.0, (_now_ms() - int(observation.get("observed_end_ms")
                                        or _now_ms())) / 1000.0)
    if idade_s > ACK_MAX_READ_AGE_S:
        falha = await register_validation_failure(reason=ACK_READ_TOO_OLD,
                                                  context=context)
        return {"ok": False, "reason_code": ACK_READ_TOO_OLD, "valid": [],
                "invalidated": [], "closed": [], "waiting": [],
                "failure_persisted": falha["ok"]}
    escopo_obs = str(observation.get("scope") or SCOPE_ACCOUNT)
    if escopo_obs != str(context.get("scope")):
        return {"ok": False, "reason_code": STALE_CONTEXT, "valid": [],
                "invalidated": [], "closed": [], "waiting": [],
                "detail": "escopo da observação difere do contexto"}
    conta = str(context.get("account_scope") or "")
    if str(observation.get("account_scope") or conta) != conta:
        return {"ok": False, "reason_code": ACK_NO_ACCOUNT, "valid": [],
                "invalidated": [], "closed": [], "waiting": []}
    atual = current_account_scope()
    if atual != conta:
        falha = await register_validation_failure(reason=ACK_NO_ACCOUNT,
                                                  context=context)
        return {"ok": False, "reason_code": ACK_NO_ACCOUNT, "valid": [],
                "invalidated": [], "closed": [], "waiting": [],
                "failure_persisted": falha["ok"]}

    # ── Decisão (fora da transação; ordens consultadas aqui) ──────────────
    posicoes = observation.get("positions") or []
    planejadas, candidatos_prova = [], []
    for instantaneo in context.get("acks") or ():
        chave = symbol_key(instantaneo.get("symbol"))
        perna = _leg_for_symbol(posicoes, chave)
        if not perna["ok"]:
            if perna["reason_code"] != ACK_POSITION_ABSENT:
                planejadas.append((instantaneo, STATE_INVALIDATED,
                                   "pernas ambíguas no símbolo"))
                continue
            ordens = await symbol_has_live_orders(instantaneo.get("symbol"))
            if not ordens.get("ok"):
                planejadas.append((instantaneo, STATE_WAITING_ORDERS,
                                   "ordens do símbolo não confirmadas"))
            elif ordens.get("live"):
                planejadas.append((instantaneo, STATE_WAITING_ORDERS,
                                   f"{ordens.get('count')} ordem(ns) do operador viva(s)"))
            else:
                planejadas.append((instantaneo, STATE_CLOSED,
                                   "posição e ordens ausentes em leitura fresca"))
            continue
        posicao = perna["position"]
        impressao = position_fingerprint(
            account_scope=conta, exchange=EXCHANGE_BINANCE,
            market=MARKET_USDM_FUTURES, symbol=posicao["symbol"],
            side=posicao["side"], position_side=posicao["position_side"],
            qty=posicao["qty"], entry_price=posicao["entry_price"],
            update_time_ms=posicao["update_time_ms"],
            contract_version=instantaneo.get("contract_version") or _contract_version())
        if impressao is not None and impressao == instantaneo.get("fingerprint"):
            if str(instantaneo.get("state")) == STATE_ACTIVE:
                candidatos_prova.append(instantaneo)
            # INVALIDATED/WAITING com fingerprint coincidente NÃO reativam:
            # só nova confirmação administrativa reativa.
        else:
            planejadas.append((instantaneo, STATE_INVALIDATED,
                               "identidade divergente da reconhecida"))

    return await _commit_validation(context=context, observation=observation,
                                    transitions=planejadas,
                                    proof_candidates=candidatos_prova)


async def _commit_validation(*, context, observation, transitions,
                             proof_candidates) -> Dict[str, Any]:
    """Transições, revogações, provas e recuperação em UM commit, com CAS."""
    try:
        from db import DB_ENABLED, get_session
        if not DB_ENABLED:
            return {"ok": True, "reason_code": "REGISTRY_DISABLED", "valid": [],
                    "invalidated": [], "closed": [], "waiting": []}
        from models.manual_position_ack import ManualPositionAcknowledgement as Ack
        from services.entry_intent_service import (acquire_risk_lock,
                                                   current_manual_generation,
                                                   set_manual_validation_blocked)
        from sqlalchemy import select
        conta = str(context.get("account_scope"))
        agora = _now()
        carimbo = int(observation.get("observed_end_ms") or _now_ms())
        escopo = str(context.get("scope"))
        validos, invalidados, fechados, esperando, perdidos = [], [], [], [], []
        async with get_session() as session:
            async with session.begin():
                await acquire_risk_lock(session)
                epoca = await current_manual_generation(
                    session, account_scope=conta, exchange=EXCHANGE_BINANCE,
                    market=MARKET_USDM_FUTURES)
                capturada = context.get("manual_generation")
                if capturada is None or int(epoca) != int(capturada):
                    return {"ok": False, "reason_code": STALE_CONTEXT, "valid": [],
                            "invalidated": [], "closed": [], "waiting": [],
                            "detail": "época manual avançou após a captura"}
                ids = sorted({int(i["id"]) for i in (context.get("acks") or ())})
                linhas = {}
                if ids:
                    linhas = {int(l.id): l for l in (await session.execute(
                        select(Ack).where(Ack.id.in_(ids)).order_by(Ack.id)
                        .with_for_update())).scalars().all()}
                # Linha BLOQUEANTE criada/alterada DEPOIS da captura invalida o
                # ciclo inteiro: ela não pertence a esta observação.
                atuais = (await session.execute(
                    select(Ack).where(Ack.account_scope == conta,
                                      Ack.exchange == EXCHANGE_BINANCE,
                                      Ack.market == MARKET_USDM_FUTURES,
                                      Ack.state.in_(list(BLOCKING_STATES)))
                    .order_by(Ack.id))).scalars().all()
                if escopo == SCOPE_SYMBOL:
                    alvo = context.get("symbol_key")
                    atuais = [l for l in atuais if symbol_key(l.symbol) == alvo]
                if {int(l.id) for l in atuais} != set(ids):
                    return {"ok": False, "reason_code": STALE_CONTEXT, "valid": [],
                            "invalidated": [], "closed": [], "waiting": [],
                            "detail": "conjunto de reconhecimentos mudou durante o GET"}

                def cas_ok(instantaneo) -> bool:
                    linha = linhas.get(int(instantaneo["id"]))
                    return (linha is not None
                            and int(linha.revision or 0) == int(instantaneo["revision"])
                            and str(linha.state) == str(instantaneo["state"])
                            and str(linha.fingerprint) == str(instantaneo["fingerprint"]))

                for instantaneo, destino, motivo in transitions:
                    if not cas_ok(instantaneo):
                        perdidos.append(int(instantaneo["id"]))
                        continue
                    linha = linhas[int(instantaneo["id"])]
                    linha.state = destino
                    linha.revision = int(linha.revision or 0) + 1
                    linha.updated_at = agora
                    linha.ended_reason = _short(motivo, 120)
                    linha.ended_at = agora if destino in ENDED_STATES else None
                    # Mudança de estado/identidade REVOGA a prova no MESMO commit.
                    _revoke_proof(linha, agora)
                    (fechados if destino == STATE_CLOSED else
                     esperando if destino == STATE_WAITING_ORDERS else
                     invalidados).append(int(instantaneo["id"]))

                for instantaneo in proof_candidates:
                    if not cas_ok(instantaneo):
                        perdidos.append(int(instantaneo["id"]))
                        continue
                    linha = linhas[int(instantaneo["id"])]
                    if str(linha.state) != STATE_ACTIVE:
                        perdidos.append(int(linha.id))
                        continue
                    linha.validated_at_ms = carimbo
                    linha.validation_scope = escopo
                    linha.validation_account = conta
                    linha.validated_revision = int(linha.revision or 0)
                    linha.validated_generation = int(epoca)
                    linha.updated_at = agora
                    validos.append(int(linha.id))

                recuperou = False
                if escopo == SCOPE_ACCOUNT and not perdidos:
                    # Recuperação só com TUDO coerente: nenhuma linha bloqueante
                    # sem prova commitada e nenhum CAS perdido.
                    restantes = [l for l in atuais
                                 if str(l.state) in BLOCKING_STATES
                                 and int(l.id) not in validos
                                 and int(l.id) not in invalidados + esperando]
                    pendentes = [l for l in atuais if str(l.state) == STATE_ACTIVE
                                 and int(l.id) not in validos]
                    bloqueantes_restantes = [l for l in atuais
                                             if int(l.id) in invalidados + esperando]
                    if not restantes and not pendentes and not bloqueantes_restantes:
                        await set_manual_validation_blocked(
                            session, account_scope=conta, exchange=EXCHANGE_BINANCE,
                            market=MARKET_USDM_FUTURES, blocked=False)
                        recuperou = True
        return {"ok": True, "reason_code": "REVALIDATED", "valid": validos,
                "invalidated": invalidados, "closed": fechados,
                "waiting": esperando, "stale": perdidos, "scope": escopo,
                "account_unblocked": recuperou, "persisted": True}
    except Exception as exc:  # noqa: BLE001
        log.error(f"[manual-ack] commit de validação falhou: "
                  f"{type(exc).__name__}: {exc}")
        falha = await register_validation_failure(
            reason=f"persistência: {type(exc).__name__}", context=context)
        return {"ok": False, "reason_code": "MANUAL_ACK_PERSISTENCE_FAILED",
                "valid": [], "invalidated": [], "closed": [], "waiting": [],
                "failure_persisted": falha["ok"]}


async def publish_validation_proof(*, context, observation, validated_ids,
                                   validated_at_ms) -> Dict[str, Any]:
    """Publica prova SOMENTE para ACTIVE cujo CAS do contexto ainda vale.

    Substitui a autorização por lista nua de IDs: sem contexto/evidência não há
    publicação, e `valid` contém apenas linhas efetivamente COMMITADAS.
    """
    alvo = {int(i) for i in (validated_ids or ())}
    candidatos = [i for i in (context.get("acks") or ())
                  if int(i["id"]) in alvo and str(i.get("state")) == STATE_ACTIVE]
    if not candidatos:
        return {"ok": True, "valid": [], "stale": sorted(alvo),
                "reason_code": "NO_PROOF_CANDIDATES"}
    return await _commit_validation(context=context, observation=observation,
                                    transitions=[], proof_candidates=candidatos)


async def _transition_acks(transicoes: List[tuple]) -> Dict[str, Any]:
    """Transições diretas com CAS (uso interno/administrativo).

    `transicoes` = [(id, revisao_lida, estado_final, motivo)]. Mudança de estado
    SEMPRE revoga a prova no mesmo commit.
    """
    if not transicoes:
        return {"ok": True, "changed": [], "by_state": [], "stale": []}
    try:
        from db import get_session
        from models.manual_position_ack import ManualPositionAcknowledgement as Ack
        from services.entry_intent_service import acquire_risk_lock
        from sqlalchemy import select
        agora = _now()
        alterados, por_estado, obsoletos = [], [], []
        async with get_session() as session:
            async with session.begin():
                await acquire_risk_lock(session)
                ids = sorted({int(i) for i, _r, _s, _m in transicoes})
                linhas = {linha.id: linha for linha in (await session.execute(
                    select(Ack).where(Ack.id.in_(ids)).order_by(Ack.id)
                    .with_for_update())).scalars().all()}
                for identificador, revisao, estado_final, motivo in transicoes:
                    linha = linhas.get(int(identificador))
                    if linha is None:
                        obsoletos.append(int(identificador))
                        continue
                    if revisao is not None and int(linha.revision or 0) != int(revisao):
                        obsoletos.append(int(identificador))
                        continue
                    if linha.state in ENDED_STATES:
                        obsoletos.append(int(identificador))
                        continue
                    linha.state = estado_final
                    linha.revision = int(linha.revision or 0) + 1
                    linha.updated_at = agora
                    linha.ended_reason = _short(motivo, 120)
                    linha.ended_at = agora if estado_final in ENDED_STATES else None
                    _revoke_proof(linha, agora)
                    alterados.append(linha.id)
                    por_estado.append((linha.id, estado_final))
        return {"ok": True, "changed": alterados, "by_state": por_estado,
                "stale": obsoletos}
    except Exception as exc:  # noqa: BLE001
        log.error(f"[manual-ack] transição falhou: {type(exc).__name__}: {exc}")
        return {"ok": False, "changed": [], "by_state": [], "stale": [],
                "detail": type(exc).__name__}


async def _end_acks(ids: List[int], *, state_final: str,
                    reason: Optional[str] = None,
                    reasons: Optional[dict] = None) -> Dict[str, Any]:
    """Compat: encerra sem CAS (chamadores antigos). Mesmo contrato de retorno."""
    return await _transition_acks([
        (int(i), None, state_final, (reasons or {}).get(i) or reason or state_final)
        for i in (ids or ())])


def ownership_from_rows(rows: Any, *, account_scope: Any, exchange: Any,
                        market: Any, symbol: Any, action: str = "mutate",
                        require_fresh_proof: bool = False,
                        manual_generation: Any = None,
                        account_blocked: bool = False) -> Dict[str, Any]:
    """Veredicto de ownership a partir de linhas JÁ lidas NA transação. PURO."""
    chave = symbol_key(symbol)
    conta = str(account_scope or "").strip()
    if not conta or "/" not in chave:
        return {"allowed": False, "reason_code": GUARD_SYMBOL_UNKNOWN,
                "action": action, "detail": "conta/símbolo canônicos indisponíveis"}
    alvo = []
    bloqueantes = []
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
        bloqueantes.append(dados)
        if symbol_key(dados.get("symbol")) == chave:
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
        if account_blocked:
            return {"allowed": False, "reason_code": GUARD_ACCOUNT_BLOCKED,
                    "action": action, "symbol": canonical_symbol(chave),
                    "detail": "validação manual da conta bloqueada"}
        vencidos = [d.get("id") for d in bloqueantes
                    if not proof_is_valid(d, account_scope=conta,
                                          manual_generation=manual_generation)]
        if vencidos:
            return {"allowed": False, "reason_code": GUARD_PROOF_STALE,
                    "action": action, "symbol": canonical_symbol(chave),
                    "ack_ids": vencidos,
                    "detail": "reconhecimento manual sem validação completa"}
    return {"allowed": True, "reason_code": GUARD_OK, "action": action,
            "symbol": canonical_symbol(chave)}


async def check_ownership_in_session(session, *, account_scope: Any,
                                     exchange: Any, market: Any, symbol: Any,
                                     action: str = "reserve",
                                     require_fresh_proof: bool = True
                                     ) -> Dict[str, Any]:
    """Ownership DENTRO da transação da admissão (só banco)."""
    try:
        from models.manual_position_ack import ManualPositionAcknowledgement as Ack
        from services.entry_intent_service import current_manual_generation
        from sqlalchemy import select
        linhas = (await session.execute(
            select(Ack).where(Ack.state.in_(list(BLOCKING_STATES)))
            .order_by(Ack.id))).scalars().all()
        epoca, bloqueada = await _manual_state_in_session(
            session, account_scope=account_scope, exchange=exchange, market=market)
    except Exception as exc:  # noqa: BLE001
        log.error(f"[manual-ack] ownership na transação falhou: "
                  f"{type(exc).__name__}: {exc}")
        return {"allowed": False, "reason_code": GUARD_REGISTRY_UNAVAILABLE,
                "action": action, "detail": type(exc).__name__}
    if pending_validation_failure() is not None and require_fresh_proof \
            and str(action) not in PROTECTIVE_ACTIONS:
        return {"allowed": False, "reason_code": GUARD_ACCOUNT_BLOCKED,
                "action": action, "detail": "falha de validação pendente"}
    return ownership_from_rows(
        [linha.to_public() for linha in linhas], account_scope=account_scope,
        exchange=exchange, market=market, symbol=symbol, action=action,
        require_fresh_proof=require_fresh_proof, manual_generation=epoca,
        account_blocked=bloqueada)


async def _manual_state_in_session(session, *, account_scope, exchange, market):
    """(época manual, bloqueada) lidas NA transação corrente."""
    from models.account_margin_epoch import AccountMarginEpoch as Epoch
    from sqlalchemy import select
    linha = (await session.execute(
        select(Epoch).where(Epoch.account_scope == str(account_scope or ""),
                            Epoch.exchange == str(exchange or "").lower(),
                            Epoch.market == str(market or "").lower()))).scalar_one_or_none()
    if linha is None:
        return 0, True
    return (int(linha.manual_validation_generation or 0),
            bool(linha.manual_validation_blocked))


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
