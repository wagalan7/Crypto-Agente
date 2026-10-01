"""P03 — reserva transacional da entrada econômica (SAFETY_FIX).

Contrato: NENHUM POST de entrada antes do commit da intenção. A exclusividade
é do PostgreSQL (chave única + compare-and-set), não da memória do processo.

Quem envia precisa do lease em `SENDING`. Lease expirado NÃO prova que a
tentativa anterior não enviou: a intenção vai para `UNKNOWN` e só a consulta
pelo MESMO `client_order_id` (reconciliador P03) pode resolvê-la. Nada aqui
reenvia ordem, cancela, apaga intenção ou presume ausência por TTL.

Sem I/O de exchange e sem transação aberta durante I/O: cada operação abre,
decide e fecha sua própria transação curta.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
import logging
from typing import Any, Dict, Mapping, Optional, Tuple

log = logging.getLogger(__name__)

from sqlalchemy import func, or_, select, text, update

from models.entry_intent import (
    EntryIntent, PENDING_STATES, STATE_CONFIRMED, STATE_RESERVED,
    STATE_SENDING, STATE_TERMINAL, STATE_UNKNOWN,
)

SCHEMA_VERSION = "p03.intent.v1"
#: Mesma advisory lock transacional do P03/risco: a admissão de capacidade de
#: decisões CONCORRENTES precisa ser serializada com arm/release da pausa.
RISK_LOCK_KEY = 917283
DEFAULT_LEASE_SECONDS = 90
MAX_CLIENT_ORDER_ID = 36
#: Vocabulário fechado das decisões de reserva.
RESERVED_NEW = "RESERVED_NEW"
RESERVED_RESUMED = "RESERVED_RESUMED"
BLOCKED_IN_FLIGHT = "BLOCKED_IN_FLIGHT"
BLOCKED_UNKNOWN = "BLOCKED_UNKNOWN"
BLOCKED_CONFIRMED = "BLOCKED_CONFIRMED"
BLOCKED_TERMINAL = "BLOCKED_TERMINAL"
BLOCKED_CAPACITY = "BLOCKED_CAPACITY"
CONFLICT_PAYLOAD = "CONFLICT_PAYLOAD"
UNAVAILABLE = "UNAVAILABLE"
GRANTED = (RESERVED_NEW, RESERVED_RESUMED)


def _finite(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _label(value, max_len: int = 40) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > max_len:
        return None
    return value if re.fullmatch(r"[A-Za-z0-9_:+./-]+", value) else None


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EntryIdentity:
    """Identidade da DECISÃO. Não inclui qty, score, preço mutável nem retry."""

    account_ref: str
    exchange: str
    symbol: str
    quote: str
    side: str
    position_side: str
    timeframe: str
    playbook: str
    playbook_version: str
    purpose: str
    trigger_candle_ms: int

    def __post_init__(self) -> None:
        # `account_ref` carrega a referência OPACA da conta (sha256 hex do
        # contrato contábil R05C): 64 caracteres, sem truncar nem colidir.
        if _label(self.account_ref, 64) is None:
            raise ValueError("identidade inválida: account_ref")
        for field in ("exchange", "symbol", "quote", "side",
                      "position_side", "timeframe", "playbook", "playbook_version", "purpose"):
            if _label(getattr(self, field), 50) is None:
                raise ValueError(f"identidade inválida: {field}")
        if self.side not in ("long", "short"):
            raise ValueError("side deve ser long ou short")
        if isinstance(self.trigger_candle_ms, bool) or not isinstance(self.trigger_candle_ms, int):
            raise ValueError("trigger_candle_ms inteiro obrigatório")
        if self.trigger_candle_ms <= 0:
            raise ValueError("trigger_candle_ms deve ser positivo")

    @property
    def setup_id(self) -> str:
        """Setup estável: símbolo, TF, lado e vela/gatilho da decisão."""
        return _digest([self.symbol, self.timeframe, self.side, self.trigger_candle_ms,
                        self.playbook, self.playbook_version])

    @property
    def intent_key(self) -> str:
        return _digest([SCHEMA_VERSION, self.account_ref, self.exchange, self.symbol,
                        self.quote, self.side, self.position_side, self.timeframe,
                        self.playbook, self.playbook_version, self.purpose,
                        self.trigger_candle_ms, self.setup_id])

    @property
    def client_order_id(self) -> str:
        """Determinístico, dentro do charset/limite do contrato da exchange."""
        return f"cw-{self.intent_key[:20]}"[:MAX_CLIENT_ORDER_ID]


def payload_fingerprint(payload: dict) -> str:
    """Conteúdo MATERIAL da decisão (preços/proteções), sem qty nem relógio."""
    fields = ("entry", "stop_loss", "tp1", "tp2", "leverage")
    values = {}
    for field in fields:
        value = payload.get(field)
        if value is None:
            values[field] = None
            continue
        number = _finite(value)
        if number is None:
            raise ValueError(f"payload inválido: {field}")
        values[field] = round(number, 10)
    return _digest(values)


@dataclass(frozen=True)
class Capacity:
    """Admissão atômica entre decisões distintas (slots e risco aberto)."""

    risk_usd: float = 0.0
    max_open_positions: Optional[int] = None
    max_open_risk_usd: Optional[float] = None
    open_positions: int = 0
    open_risk_usd: float = 0.0


@dataclass(frozen=True)
class DailyBudget:
    """Orçamento de PERDA do dia, medido FORA da transação e verificado DENTRO
    dela, junto com a admissão das reservas.

    `base_usd` é o pior cenário SEM reservas e SEM a proposta (P&L da fonte
    selecionada − exposição aberta − custos já conhecidos). As reservas de
    OUTRAS intenções são lidas sob a mesma lock, então duas decisões não
    consomem juntas a última margem. `complete=False` (ou parcela ausente)
    BLOQUEIA: desconhecido nunca vira zero.
    """

    base_usd: Optional[float] = None
    limit_usd: Optional[float] = None
    complete: bool = False


#: A observação de carteira usada nesta proposta ficou OBSOLETA: houve mudança
#: local na interpretação de margem (reserva criada/alterada, intenção virando
#: posição, liberação/terminal, recovery) entre a leitura e a admissão.
MARGIN_SUPERSEDED = "MARGIN_OBSERVATION_SUPERSEDED"
#: O símbolo pertence a uma posição manual reconhecida (ou o registro está
#: ilegível/ambíguo): a admissão NEGA dentro da própria transação.
OWNERSHIP_BLOCKED = "MANUAL_POSITION_SYMBOL_BLOCKED"


@dataclass(frozen=True)
class MarginGate:
    """MARGEM realmente disponível na conta COMPARTILHADA com o operador.

    Limite INDEPENDENTE do orçamento nominal do bot: caber no risco aprovado
    não prova que há saldo livre para abrir. `available_usd` já reflete a
    margem usada na conta (inclusive a da posição manual) — ela NÃO é somada de
    volta, e a margem das posições abertas NÃO é descontada de novo.

    `as_of_ms` é a prova temporal da leitura feita FORA da transação; ela é
    conferida DENTRO dela. Parcela ausente/incompleta BLOQUEIA: desconhecido
    nunca vira zero nem estimativa favorável.
    """

    available_usd: Optional[float] = None
    required_usd: float = 0.0
    as_of_ms: Optional[int] = None
    max_age_s: float = 30.0
    complete: bool = False
    #: Identidade da carteira observada. A admissão só aceita observação da
    #: MESMA conta/exchange/mercado da intenção.
    account_ref: Optional[str] = None
    exchange: Optional[str] = None
    market: Optional[str] = None
    #: Geração (época) vigente quando a carteira foi lida. Geração diferente na
    #: admissão significa observação SUPERADA — nega sem POST.
    generation: Optional[int] = None
    #: Janela REAL da obtenção e qualidade declarada pela origem.
    observed_start_ms: Optional[int] = None
    observed_end_ms: Optional[int] = None
    quality: Optional[str] = None


@dataclass(frozen=True)
class Reservation:
    decision: str
    intent_key: Optional[str] = None
    client_order_id: Optional[str] = None
    state: Optional[str] = None
    reason: Optional[str] = None
    #: Geração de margem RESULTANTE desta operação. O dispatch confere este
    #: token antes de enviar: mudança concorrente exige nova admissão.
    generation: Optional[int] = None

    @property
    def granted(self) -> bool:
        return self.decision in GRANTED


#: Tolerância de relógio (ms) ao comparar o carimbo da carteira com o agora.
_MARGIN_CLOCK_SKEW_MS = 2_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _margin_identity(account_ref, exchange, market) -> tuple:
    return (str(account_ref or ""), str(exchange or "").lower(),
            str(market or "usdm_futures").lower())


#: ORDEM ÚNICA DE LOCKS deste protocolo (evita a inversão que o PostgreSQL
#: detectava como `deadlock detected`):
#:
#:   1. latch local (quando houver) ANTES de abrir a transação;
#:   2. `BEGIN` → `pg_advisory_xact_lock(917283)` ANTES de qualquer leitura
#:      decisória ou row lock;
#:   3. linha da ÉPOCA da conta antes das linhas de intenção;
#:   4. linhas de reconhecimento/intenção em ordem determinística de id/chave;
#:   5. escrita/prova/CAS → commit único → liberação.
#:
#: Helpers internos recebem a sessão JÁ aberta: nenhum deles pode abrir outra
#: conexão enquanto esta segura a advisory lock.
_LOCK_SQL = text("SELECT pg_advisory_xact_lock(:k)")


async def acquire_risk_lock(session) -> None:
    """Passo 2 da ordem única. Idempotente dentro da mesma transação."""
    await session.execute(_LOCK_SQL, {"k": RISK_LOCK_KEY})


async def _lock_epoch_row(session, *, account_ref: str, exchange: str,
                          market: str = "usdm_futures"):
    """Passo 3: trava (ou cria) a linha da época ANTES das intenções."""
    from models.account_margin_epoch import AccountMarginEpoch as Epoch
    conta, corretora, mercado = _margin_identity(account_ref, exchange, market)
    linha = (await session.execute(
        select(Epoch).where(Epoch.account_scope == conta, Epoch.exchange == corretora,
                            Epoch.market == mercado).with_for_update())).scalar_one_or_none()
    if linha is None:
        linha = Epoch(account_scope=conta, exchange=corretora, market=mercado,
                      generation=0, manual_validation_generation=0,
                      manual_validation_blocked=True, updated_at=_now())
        session.add(linha)
        await session.flush()
    return linha


async def current_margin_generation(session, *, account_ref: str, exchange: str,
                                    market: str = "usdm_futures") -> int:
    """Geração (época) vigente da interpretação de margem daquela conta.

    Cria a linha com geração 0 na primeira consulta. DEVE ser chamada sob a
    lock `917283` quando o valor for usado para decidir.
    """
    linha = await _lock_epoch_row(session, account_ref=account_ref,
                                  exchange=exchange, market=market)
    return int(linha.generation or 0)


async def _bump_margin_generation(session, *, account_ref: str, exchange: str,
                                  market: str = "usdm_futures") -> int:
    """Incrementa a geração NA MESMA TRANSAÇÃO da mudança econômica.

    Toda carteira observada ANTES desta transação passa a ser obsoleta — é esse
    o vínculo que impede gastar duas vezes o mesmo saldo livre quando a reserva
    sai da soma de pendentes ao virar posição.
    """
    linha = await _lock_epoch_row(session, account_ref=account_ref,
                                  exchange=exchange, market=market)
    linha.generation = int(linha.generation or 0) + 1
    linha.updated_at = _now()
    await session.flush()
    return int(linha.generation)


async def current_manual_generation(session, *, account_scope: str, exchange: str,
                                    market: str = "usdm_futures") -> int:
    """Época da VALIDAÇÃO MANUAL daquela conta (contador INDEPENDENTE do
    financeiro). Renovar prova manual não mexe na geração financeira, e uma
    carteira financeira recente não mascara falha de validação manual."""
    linha = await _lock_epoch_row(session, account_ref=account_scope,
                                  exchange=exchange, market=market)
    return int(linha.manual_validation_generation or 0)


async def bump_manual_generation(session, *, account_scope: str, exchange: str,
                                 market: str = "usdm_futures",
                                 blocked: bool = True) -> int:
    """Avança a época manual e (por padrão) BLOQUEIA a conta, na transação atual."""
    linha = await _lock_epoch_row(session, account_ref=account_scope,
                                  exchange=exchange, market=market)
    linha.manual_validation_generation = int(linha.manual_validation_generation or 0) + 1
    linha.manual_validation_blocked = bool(blocked)
    linha.updated_at = _now()
    await session.flush()
    return int(linha.manual_validation_generation)


async def set_manual_validation_blocked(session, *, account_scope: str,
                                        exchange: str,
                                        market: str = "usdm_futures",
                                        blocked: bool) -> bool:
    """Marca/limpa o bloqueio DURÁVEL de validação manual na transação atual."""
    linha = await _lock_epoch_row(session, account_ref=account_scope,
                                  exchange=exchange, market=market)
    linha.manual_validation_blocked = bool(blocked)
    linha.updated_at = _now()
    await session.flush()
    return bool(linha.manual_validation_blocked)


async def bump_margin_generation_for(session_factory, *, account_ref: str,
                                     exchange: str,
                                     market: str = "usdm_futures") -> Optional[int]:
    """Incremento em transação PRÓPRIA (escritores que não têm sessão aberta).

    Falha devolve None e o chamador trata como incerteza — nunca "sem mudança".
    """
    if not account_ref:
        return None
    try:
        async with session_factory() as session:
            async with session.begin():
                await acquire_risk_lock(session)
                return await _bump_margin_generation(
                    session, account_ref=account_ref, exchange=exchange, market=market)
    except Exception:  # noqa: BLE001
        return None


def _margin_observation_matches(margin: "MarginGate", identity: "EntryIdentity",
                                generation: int) -> bool:
    """A carteira observada é DESTA conta/mercado e da geração vigente?

    Identidade divergente ou geração diferente = observação superada. Gate sem
    geração declarada também não passa: prova de concorrência é obrigatória.
    """
    if margin.generation is None:
        return False
    try:
        if int(margin.generation) != int(generation):
            return False
    except (TypeError, ValueError):
        return False
    if margin.account_ref is not None \
            and str(margin.account_ref) != str(identity.account_ref):
        return False
    if margin.exchange is not None \
            and str(margin.exchange).lower() != str(identity.exchange or "").lower():
        return False
    if margin.market is not None and str(margin.market).lower() != "usdm_futures":
        return False
    return True


async def _ownership_denial(session, identity: "EntryIdentity", action: str) -> Optional[str]:
    """Ownership DENTRO da transação da admissão (§3). Negação ⇒ motivo."""
    try:
        from services import manual_position_service as mps
        veredito = await mps.check_ownership_in_session(
            session, account_scope=identity.account_ref, exchange=identity.exchange,
            market="usdm_futures",
            symbol=f"{identity.symbol}", action=action, require_fresh_proof=True)
    except Exception:  # noqa: BLE001 — dúvida NEGA
        return OWNERSHIP_BLOCKED
    if veredito.get("allowed"):
        return None
    return OWNERSHIP_BLOCKED


async def _pending_usage(session, account_ref: str, exchange: str, *,
                         exclude_intent_key: Optional[str] = None):
    """Reservas ainda não resolvidas — capacidade não é liberada só porque o
    RealTrade ainda não nasceu.

    `exclude_intent_key` tira a PRÓPRIA decisão da soma: quem já está reservado
    seria contado duas vezes ao reavaliar a própria tentativa. Intenção já
    vinculada a RealTrade também fica de fora (o risco dela já é exposição
    aberta), e a conta/exchange delimitam a população — reserva de outra conta
    não consome este orçamento."""
    filtros = [EntryIntent.account_ref == account_ref, EntryIntent.exchange == exchange,
               EntryIntent.state.in_(PENDING_STATES), EntryIntent.real_trade_id.is_(None)]
    if exclude_intent_key:
        filtros.append(EntryIntent.intent_key != exclude_intent_key)
    row = (await session.execute(
        select(func.count(EntryIntent.intent_key), func.coalesce(func.sum(EntryIntent.reserved_risk_usd), 0.0))
        .where(*filtros))).one()
    return int(row[0] or 0), float(row[1] or 0.0)


async def _open_positions(session) -> int:
    """Posições abertas contadas DENTRO da mesma transação da admissão.

    Mesma população do teto de slots em produção (`portfolio_service`): trades
    reais da coorte automática ainda abertos.
    """
    from models.real_trade import RealTrade
    return int((await session.execute(
        select(func.count(RealTrade.id))
        .where(RealTrade.status == "open", RealTrade.source == "auto"))).scalar() or 0)


def _open_risk_from_rows(rows) -> Tuple[float, bool]:
    """Risco aberto a partir das linhas JÁ LIDAS (mesmo snapshot da admissão).

    Devolve `(risco, completo)`; linha sem entry, qty ou stop utilizável marca
    INCOMPLETO — desconhecido nunca vira zero.
    """
    total, complete = 0.0, True
    for row in rows or ():
        price, quantity = _finite(row.get("entry_price")), _finite(row.get("qty"))
        sl_current, planned_stop = row.get("sl_current_price"), row.get("planned_stop")
        stop = _finite(sl_current if sl_current is not None else planned_stop)
        if price is None or quantity is None or stop is None or price <= 0 or quantity <= 0:
            complete = False
            continue
        lado = str(row.get("side") or "long").lower()
        adverse = (price - stop) if lado == "long" else (stop - price)
        total += max(0.0, adverse) * quantity
    return total, complete


async def _admission_view(session, identity: EntryIdentity, key: str) -> Optional[dict]:
    """Visão ÚNICA e consistente da admissão (P&L, exposição, custos, reservas).

    Uma instrução, executada DEPOIS da advisory lock: posição que fecha entre
    leituras não pode sumir das duas parcelas. Falha de leitura ⇒ None, e quem
    chama BLOQUEIA — nunca admite sobre base obsoleta.
    """
    try:
        from services import financial_risk_service as frs
        visao = await frs.admission_snapshot(
            session, account_ref=identity.account_ref, exchange=identity.exchange,
            exclude_intent_key=key)
    except Exception:  # noqa: BLE001
        return None
    if visao.get("open_rows") is None:
        return None
    risco, completo = _open_risk_from_rows(visao["open_rows"])
    base = visao.get("base") if isinstance(visao.get("base"), dict) else {}
    return {"pending_count": int(visao.get("pending_count") or 0),
            "pending_risk": float(visao.get("pending_risk_usd") or 0.0),
            "pending_margin": float(visao.get("pending_margin_usd") or 0.0),
            "open_positions": int(visao.get("open_positions") or 0),
            "open_risk_usd": risco, "open_risk_complete": completo,
            "base_usd": (_finite(base.get("value")) if base.get("quality") == "OK"
                         else None),
            "base_reason": (None if base.get("quality") == "OK"
                            else (base.get("reason_code") or "DAILY_BASE_UNAVAILABLE"))}


async def _daily_base_in_session(session) -> tuple:
    """Base do dia RECALCULADA na transação da admissão.

    Devolve `(base, motivo)`: a base antiga, lida fora da lock, envelhece —
    entre ela e a lock uma reserva pode virar posição (sai do pending sem
    nunca entrar na base) ou um resultado pode ser persistido. Leitura
    indisponível ⇒ base None e motivo explícito (nunca zero).
    """
    try:
        from services import financial_risk_service as frs
        verdict = await frs.daily_base_in_session(session)
    except Exception:  # noqa: BLE001
        return None, "DAILY_BASE_UNAVAILABLE"
    if verdict.get("quality") != "OK":
        return None, (verdict.get("reason_code") or "DAILY_BASE_UNAVAILABLE")
    base = _finite(verdict.get("value"))
    return (base, None) if base is not None else (None, "DAILY_BASE_UNAVAILABLE")


def _budget_reason(budget: Optional[DailyBudget], pending_risk: float,
                   proposed_risk: float, base_usd: Any = None) -> Optional[str]:
    """Limite DIÁRIO com as reservas das OUTRAS intenções incluídas.

    `base_usd` é a base RECALCULADA sob a lock; sem ela o veredicto usa a base
    informada pelo caller, que pode estar velha. Atingir o limite já bloqueia
    (>=, não >), como no gate financeiro. Sem orçamento informado não há
    veredicto aqui (contrato legado/cutover desligado); orçamento informado e
    incompleto BLOQUEIA."""
    if budget is None:
        return None
    base, limit = _finite(budget.base_usd), _finite(budget.limit_usd)
    # Contrato do caller primeiro: orçamento declarado incompleto (ou sem base/
    # limite) continua BLOQUEANDO, mesmo que a base seja recalculável aqui.
    if not budget.complete or base is None or limit is None or limit <= 0:
        return "DAILY_BUDGET_UNKNOWN"
    recalculada = _finite(base_usd)
    if recalculada is not None:
        base = recalculada          # a base da transação manda sobre a antiga
    proposto = _finite(proposed_risk)
    if proposto is None or proposto < 0:
        return "DAILY_BUDGET_UNKNOWN"
    pendente = _finite(pending_risk)
    if pendente is None:
        return "DAILY_BUDGET_UNKNOWN"
    worst = base - abs(pendente) - proposto
    return "DAILY_LOSS_LIMIT" if worst <= -limit else None


def _margin_reason(margin: Optional[MarginGate], pending_margin: float,
                   *, now_ms: Optional[int] = None) -> Optional[str]:
    """Bloqueia quando a margem livre REAL não cobre esta proposta.

    As margens já reservadas por OUTRAS intenções pendentes entram na conta —
    lidas sob a MESMA lock —, então duas propostas concorrentes não gastam o
    mesmo saldo livre. Sem gate informado não há veredicto (contrato legado).
    """
    if margin is None:
        return None
    disponivel = _finite(margin.available_usd)
    if not margin.complete or disponivel is None:
        return "FREE_MARGIN_UNKNOWN"
    if margin.as_of_ms is None:
        return "FREE_MARGIN_UNKNOWN"
    if disponivel < 0:
        return "FREE_MARGIN_UNKNOWN"
    if margin.quality is not None and str(margin.quality).lower() != "live":
        # Cache/stale/rate-limited não é prova de saldo atual.
        return "FREE_MARGIN_STALE"
    agora = int(now_ms if now_ms is not None else _now().timestamp() * 1000)
    idade_ms = agora - int(margin.as_of_ms)
    if idade_ms < -_MARGIN_CLOCK_SKEW_MS:
        # Carimbo no FUTURO é incoerente: não se aceita prova impossível.
        return "FREE_MARGIN_STALE"
    idade_s = max(0.0, idade_ms / 1000.0)
    limite = _finite(margin.max_age_s)
    if limite is None or limite <= 0 or idade_s > limite:
        # Carteira lida antes da transição intenção → ordem → posição não vale
        # como atual: o caller refaz a leitura ou bloqueia.
        return "FREE_MARGIN_STALE"
    requerido = _finite(margin.required_usd)
    if requerido is None or requerido < 0:
        return "FREE_MARGIN_UNKNOWN"
    reservado = _finite(pending_margin)
    if reservado is None:
        return "FREE_MARGIN_UNKNOWN"
    if disponivel - abs(reservado) - requerido < 0:
        return "INSUFFICIENT_FREE_MARGIN"
    return None


def _capacity_reason(capacity: Capacity, pending_count: int, pending_risk: float) -> Optional[str]:
    if capacity.max_open_positions is not None:
        if capacity.open_positions + pending_count + 1 > capacity.max_open_positions:
            return "MAX_OPEN_POSITIONS"
    if capacity.max_open_risk_usd is not None:
        total = capacity.open_risk_usd + pending_risk + max(0.0, capacity.risk_usd)
        if total > capacity.max_open_risk_usd:
            return "MAX_OPEN_RISK"
    return None


def decision_snapshot(payload: Any, *, qty: Any = None) -> Optional[dict]:
    """Dados POINT-IN-TIME que a reconciliação precisa depois: entrada, stop e
    quantidade planejada. Só números finitos entram — parcela inválida some, e
    a ausência continua sendo ausência (nada é estimado depois)."""
    source = payload if isinstance(payload, Mapping) else {}
    snapshot = {}
    for chave, bruto in (("entry", source.get("entry")),
                         ("stop_loss", source.get("stop_loss")),
                         ("qty", qty if qty is not None else source.get("qty"))):
        valor = _finite(bruto)
        if valor is not None:
            snapshot[chave] = valor
    return snapshot or None


async def reserve(session_factory, identity: EntryIdentity, payload: dict, *,
                  owner: str, lease_seconds: int = DEFAULT_LEASE_SECONDS,
                  capacity: Optional[Capacity] = None,
                  budget: Optional[DailyBudget] = None,
                  margin: Optional[MarginGate] = None,
                  decision: Optional[dict] = None,
                  now: Optional[datetime] = None) -> Reservation:
    """Reserva (ou recupera) a intenção em UMA transação, antes de qualquer POST.

    A admissão de capacidade E o orçamento diário são decididos sob a MESMA
    lock/transação em que a reserva é gravada: duas decisões concorrentes não
    podem consumir juntas a última margem por terem checado antes de reservar.

    Falha de banco ⇒ `UNAVAILABLE`: o chamador NÃO pode enviar ordem.
    """
    fingerprint = payload_fingerprint(payload)
    key, coid = identity.intent_key, identity.client_order_id
    moment = now or _now()
    deadline = moment + timedelta(seconds=max(1, int(lease_seconds)))
    try:
        async with session_factory() as session:
            # Serializa a admissão de capacidade entre decisões diferentes.
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": RISK_LOCK_KEY})
            # Relógio obtido DEPOIS da espera pela lock: `moment` foi capturado
            # antes dela e não prova atualidade da carteira.
            pos_lock_ms = int(_now().timestamp() * 1000)
            # Ownership DENTRO da transação, DEPOIS da lock e ANTES de conceder
            # ou gravar capacidade — vale para intenção NOVA e para retomada.
            negado = await _ownership_denial(session, identity, "reserve")
            if negado:
                await session.rollback()
                return Reservation(BLOCKED_CAPACITY, key, coid, None, negado)
            # Geração vigente: uma carteira observada antes de qualquer mudança
            # local na margem está OBSOLETA e não autoriza esta proposta.
            if margin is not None:
                atual_gen = await current_margin_generation(
                    session, account_ref=identity.account_ref,
                    exchange=identity.exchange, market="usdm_futures")
                if not _margin_observation_matches(margin, identity, atual_gen):
                    await session.rollback()
                    return Reservation(BLOCKED_CAPACITY, key, coid, None,
                                       MARGIN_SUPERSEDED)
            row = (await session.execute(
                select(EntryIntent).where(EntryIntent.intent_key == key).with_for_update()
            )).scalar_one_or_none()
            if row is None:
                # Decisão nova: admissão de capacidade sob a mesma lock.
                visao = None
                if capacity is not None or budget is not None or margin is not None:
                    # UMA leitura consistente para capacidade E orçamento.
                    visao = await _admission_view(session, identity, key)
                    if visao is None:
                        await session.rollback()
                        return Reservation(BLOCKED_CAPACITY, key, coid, None,
                                           "ADMISSION_SNAPSHOT_UNAVAILABLE")
                if capacity is not None:
                    open_positions = max(int(capacity.open_positions or 0),
                                         visao["open_positions"])
                    caller_risk = _finite(capacity.open_risk_usd)
                    open_risk = max(visao["open_risk_usd"],
                                    caller_risk if caller_risk is not None else 0.0)
                    capacity = Capacity(
                        risk_usd=capacity.risk_usd, max_open_positions=capacity.max_open_positions,
                        max_open_risk_usd=capacity.max_open_risk_usd,
                        open_positions=open_positions, open_risk_usd=open_risk)
                    if capacity.max_open_risk_usd is not None and not visao["open_risk_complete"]:
                        # Risco aberto incompleto: nada de fabricar zero para caber.
                        await session.rollback()
                        return Reservation(BLOCKED_CAPACITY, key, coid, None, "OPEN_RISK_UNKNOWN")
                    denial = _capacity_reason(capacity, visao["pending_count"],
                                              visao["pending_risk"])
                    if denial:
                        await session.rollback()
                        return Reservation(BLOCKED_CAPACITY, key, coid, None, denial)
                # Orçamento diário com as reservas das OUTRAS intenções incluídas
                # e a base do dia do MESMO snapshot desta transação.
                if budget is not None:
                    proposto = max(0.0, _finite(capacity.risk_usd) or 0.0) if capacity else 0.0
                    denial = visao["base_reason"] or _budget_reason(
                        budget, visao["pending_risk"], proposto, visao["base_usd"])
                    if denial:
                        await session.rollback()
                        return Reservation(BLOCKED_CAPACITY, key, coid, None, denial)
                # Margem REAL da conta compartilhada: limite independente do
                # orçamento nominal. Passar em um não dispensa o outro.
                if margin is not None:
                    denial = _margin_reason(margin, visao["pending_margin"],
                                            now_ms=pos_lock_ms)
                    if denial:
                        await session.rollback()
                        return Reservation(BLOCKED_CAPACITY, key, coid, None, denial)
                nova_linha = EntryIntent(
                    intent_key=key, client_order_id=coid, account_ref=identity.account_ref,
                    exchange=identity.exchange, symbol=identity.symbol, quote=identity.quote,
                    side=identity.side, position_side=identity.position_side,
                    timeframe=identity.timeframe, playbook=identity.playbook,
                    playbook_version=identity.playbook_version, purpose=identity.purpose,
                    setup_id=identity.setup_id, trigger_candle_ms=identity.trigger_candle_ms,
                    payload_fingerprint=fingerprint, state=STATE_RESERVED, reason=None,
                    lease_owner=owner, lease_expires_at=deadline, attempts=1, dispatches=0,
                    reserved_risk_usd=max(0.0, float(capacity.risk_usd) if capacity else 0.0),
                    reserved_margin_usd=max(0.0, _finite(margin.required_usd) or 0.0)
                    if margin is not None else 0.0,
                    # Gravado ANTES de qualquer POST: depois do envio não há de
                    # onde recuperar stop/qty da decisão que originou a ordem.
                    decision_payload=decision_snapshot(decision if decision is not None
                                                       else payload),
                    real_trade_id=None, created_at=moment, updated_at=moment,
                    resolved_at=None)
                session.add(nova_linha)
                # A reserva MUDA a margem representada no banco: toda carteira
                # observada antes deste commit fica obsoleta. A geração
                # RESULTANTE é GRAVADA na própria linha antes do commit — o
                # valor devolvido é exatamente o persistido.
                nova_geracao = await _bump_margin_generation(
                    session, account_ref=identity.account_ref,
                    exchange=identity.exchange, market="usdm_futures")
                nova_linha.margin_generation = nova_geracao
                await session.flush()
                await session.commit()
                return Reservation(RESERVED_NEW, key, coid, STATE_RESERVED,
                                   generation=nova_geracao)

            # Lidos ANTES de qualquer rollback: após o rollback o ORM expira a
            # instância e reler atributo dispararia IO preguiçoso.
            current_state, current_reason = row.state, row.reason
            current_fingerprint, lease_owner = row.payload_fingerprint, row.lease_owner
            lease_expires_at = row.lease_expires_at
            if current_fingerprint != fingerprint:
                # Mesma decisão com conteúdo material diferente: conflito
                # explicável, nunca uma segunda entrada silenciosa.
                await session.rollback()
                return Reservation(CONFLICT_PAYLOAD, key, coid, current_state, "PAYLOAD_DIVERGENT")
            if current_state == STATE_CONFIRMED:
                await session.rollback()
                return Reservation(BLOCKED_CONFIRMED, key, coid, current_state, current_reason)
            if current_state == STATE_UNKNOWN:
                await session.rollback()
                return Reservation(BLOCKED_UNKNOWN, key, coid, current_state, current_reason)
            if current_state == STATE_TERMINAL:
                await session.rollback()
                return Reservation(BLOCKED_TERMINAL, key, coid, current_state, current_reason)
            if current_state == STATE_SENDING:
                if lease_expires_at is not None and lease_expires_at <= moment:
                    # Lease vencido em SENDING: a tentativa anterior PODE ter
                    # enviado. Vai para UNKNOWN e exige consulta pelo client id.
                    row.state, row.reason = STATE_UNKNOWN, "LEASE_EXPIRED_AFTER_DISPATCH"
                    row.lease_owner, row.lease_expires_at = None, None
                    row.updated_at = moment
                    await session.commit()
                    return Reservation(BLOCKED_UNKNOWN, key, coid, STATE_UNKNOWN,
                                       "LEASE_EXPIRED_AFTER_DISPATCH")
                await session.rollback()
                return Reservation(BLOCKED_IN_FLIGHT, key, coid, current_state, current_reason)
            # RESERVED: ninguém despachou ainda. Só o dono do lease (ou um lease
            # vencido) pode seguir; senão outro worker está preparando o envio.
            if (lease_owner not in (None, owner)
                    and lease_expires_at is not None and lease_expires_at > moment):
                await session.rollback()
                return Reservation(BLOCKED_IN_FLIGHT, key, coid, current_state, "LEASE_HELD")
            # Retomada da MESMA decisão: se a tentativa agora carrega risco
            # MAIOR (preço/qty adversos), a diferença passa pela mesma admissão —
            # confiar no valor antigo, menor, admitiria risco nunca aprovado.
            if capacity is not None or budget is not None or margin is not None:
                novo = max(0.0, _finite(capacity.risk_usd) or 0.0) if capacity else 0.0
                antigo = max(0.0, _finite(row.reserved_risk_usd) or 0.0)
                nova_margem = (max(0.0, _finite(margin.required_usd) or 0.0)
                               if margin is not None else 0.0)
                margem_antiga = max(0.0, _finite(row.reserved_margin_usd) or 0.0)
                if novo > antigo + 1e-9 or nova_margem > margem_antiga + 1e-9:
                    denial = await _readmit(session, identity, key, capacity, budget,
                                            max(novo, antigo), margin=margin)
                    if denial:
                        await session.rollback()
                        return Reservation(BLOCKED_CAPACITY, key, coid, current_state, denial)
                    row.reserved_risk_usd = max(novo, antigo)
                    row.reserved_margin_usd = max(nova_margem, margem_antiga)
                    # A PRÓPRIA reserva aumentou: incrementa e usa o valor novo.
                    row.margin_generation = await _bump_margin_generation(
                        session, account_ref=identity.account_ref,
                        exchange=identity.exchange, market="usdm_futures")
                elif margin is not None:
                    # Só renovou a prova: usa a época ATUAL, sem bump — mas
                    # GRAVA, para o token devolvido ser o persistido.
                    row.margin_generation = await current_margin_generation(
                        session, account_ref=identity.account_ref,
                        exchange=identity.exchange, market="usdm_futures")
            if not row.decision_payload:
                # Linha antiga (ou criada sem os dados): completa sem sobrescrever
                # o que já estiver gravado — a decisão em si não mudou (mesmo
                # fingerprint), só passou a ser recuperável.
                row.decision_payload = decision_snapshot(
                    decision if decision is not None else payload)
            row.lease_owner, row.lease_expires_at = owner, deadline
            row.attempts = int(row.attempts or 0) + 1
            row.updated_at = moment
            resultante = row.margin_generation
            await session.commit()
            return Reservation(RESERVED_RESUMED, key, coid, STATE_RESERVED,
                               generation=resultante)
    except Exception:
        return Reservation(UNAVAILABLE, key, coid, None, "DB_UNAVAILABLE")


async def _readmit(session, identity: EntryIdentity, key: str,
                   capacity: Optional[Capacity], budget: Optional[DailyBudget],
                   risk_usd: float, *,
                   margin: Optional[MarginGate] = None) -> Optional[str]:
    """Reavalia capacidade, orçamento e MARGEM para um risco/margem MAIORES da
    MESMA decisão, dentro da transação/lock já abertas. Devolve o motivo da
    negação ou None."""
    visao = await _admission_view(session, identity, key)
    if visao is None:
        return "ADMISSION_SNAPSHOT_UNAVAILABLE"
    if margin is not None:
        denial = _margin_reason(margin, visao["pending_margin"],
                                now_ms=int(_now().timestamp() * 1000))
        if denial:
            return denial
    if capacity is not None:
        open_positions = max(int(capacity.open_positions or 0), visao["open_positions"])
        caller_risk = _finite(capacity.open_risk_usd)
        open_risk = max(visao["open_risk_usd"],
                        caller_risk if caller_risk is not None else 0.0)
        if capacity.max_open_risk_usd is not None and not visao["open_risk_complete"]:
            return "OPEN_RISK_UNKNOWN"
        # A decisão JÁ ocupa um slot: o teto de posições não conta mais um.
        alvo = Capacity(risk_usd=risk_usd, max_open_positions=capacity.max_open_positions,
                        max_open_risk_usd=capacity.max_open_risk_usd,
                        open_positions=open_positions, open_risk_usd=open_risk)
        denial = _capacity_reason(alvo, visao["pending_count"], visao["pending_risk"])
        if denial:
            return denial
    if budget is None:
        return None
    return visao["base_reason"] or _budget_reason(
        budget, visao["pending_risk"], risk_usd, visao["base_usd"])


async def admit_final_risk(session_factory, intent_key: str, *, owner: str,
                           risk_usd: float, capacity: Optional[Capacity] = None,
                           budget: Optional[DailyBudget] = None,
                           margin: Optional[MarginGate] = None,
                           now: Optional[datetime] = None) -> Reservation:
    """Admissão FINAL, imediatamente antes do POST, com o risco realmente
    proposto (preço/qty já revalidados).

    Roda sob a MESMA lock da reserva, exclui a própria reserva da soma (senão
    contaria duas vezes) e, quando aprovado, ATUALIZA o risco reservado — o
    valor antigo, menor, deixaria de refletir o que será enviado. Dúvida ou
    falha de banco NEGA: nada é enviado sem admissão."""
    moment = now or _now()
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": RISK_LOCK_KEY})
            # Relógio posterior à espera pela lock (ver `reserve`).
            pos_lock_ms = int(_now().timestamp() * 1000)
            row = (await session.execute(
                select(EntryIntent).where(EntryIntent.intent_key == intent_key).with_for_update()
            )).scalar_one_or_none()
            if row is None:
                await session.rollback()
                return Reservation(UNAVAILABLE, intent_key, None, None, "INTENT_NOT_FOUND")
            state, coid = row.state, row.client_order_id
            lease_owner, lease_expires_at = row.lease_owner, row.lease_expires_at
            if state not in (STATE_RESERVED, STATE_SENDING) or lease_owner != owner \
                    or lease_expires_at is None or lease_expires_at <= moment:
                await session.rollback()
                return Reservation(BLOCKED_IN_FLIGHT, intent_key, coid, state, "LEASE_NOT_HELD")
            identity = EntryIdentity(
                account_ref=row.account_ref, exchange=row.exchange, symbol=row.symbol,
                quote=row.quote, side=row.side, position_side=row.position_side,
                timeframe=row.timeframe, playbook=row.playbook,
                playbook_version=row.playbook_version, purpose=row.purpose,
                trigger_candle_ms=int(row.trigger_candle_ms or 0))
            # Readmissão passa pelo MESMO ownership da reserva: um
            # reconhecimento criado no intervalo não pode ser contornado.
            negado = await _ownership_denial(session, identity, "admit_final_risk")
            if negado:
                await session.rollback()
                return Reservation(BLOCKED_CAPACITY, intent_key, coid, state, negado)
            if margin is not None:
                atual_gen = await current_margin_generation(
                    session, account_ref=identity.account_ref,
                    exchange=identity.exchange, market="usdm_futures")
                if not _margin_observation_matches(margin, identity, atual_gen):
                    await session.rollback()
                    return Reservation(BLOCKED_CAPACITY, intent_key, coid, state,
                                       MARGIN_SUPERSEDED)
            proposto = _finite(risk_usd)
            if proposto is None or proposto < 0:
                await session.rollback()
                return Reservation(BLOCKED_CAPACITY, intent_key, coid, state, "PROPOSED_RISK_UNKNOWN")
            denial = await _readmit(session, identity, intent_key, capacity, budget,
                                    proposto, margin=margin)
            if denial:
                await session.rollback()
                return Reservation(BLOCKED_CAPACITY, intent_key, coid, state, denial)
            if proposto > (_finite(row.reserved_risk_usd) or 0.0) + 1e-9:
                row.reserved_risk_usd = proposto
                row.updated_at = moment
            resultante = row.margin_generation
            if margin is not None:
                # A margem FINAL (após arredondamentos) substitui a reservada
                # quando for maior: o valor antigo, menor, deixaria um intervalo
                # sem reserva entre a intenção e a posição.
                final = max(0.0, _finite(margin.required_usd) or 0.0)
                if final > (_finite(row.reserved_margin_usd) or 0.0) + 1e-9:
                    row.reserved_margin_usd = final
                    row.updated_at = moment
                    # A readmissão muda a própria reserva: incrementa e devolve
                    # a geração RESULTANTE (não invalida a si mesma).
                    resultante = await _bump_margin_generation(
                        session, account_ref=identity.account_ref,
                        exchange=identity.exchange, market="usdm_futures")
                else:
                    # SEM aumento: a prova vale a época ATUAL, não a antiga.
                    resultante = await current_margin_generation(
                        session, account_ref=identity.account_ref,
                        exchange=identity.exchange, market="usdm_futures")
                row.margin_generation = resultante
                row.updated_at = moment
            await session.flush()
            await session.commit()
            return Reservation(RESERVED_RESUMED, intent_key, coid, state,
                               generation=resultante)
    except Exception:
        return Reservation(UNAVAILABLE, intent_key, None, None, "DB_UNAVAILABLE")


def _finite_token(value) -> Optional[int]:
    """Token inteiro finito. `bool`, texto, NaN/inf e ausência NÃO são token."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not (value == value) or value in (float("inf"), float("-inf")):
            return None
        if value != int(value):
            return None
        return int(value)
    if isinstance(value, int):
        return int(value)
    return None


async def authorize_dispatch(session_factory, intent_key: str, *, owner: str,
                             expected_token, dispatch_id: Optional[str] = None,
                             now: Optional[datetime] = None) -> Dict[str, Any]:
    """AUTORIZAÇÃO FINAL do envio, em transação CURTA sob a lock `917283`.

    Lê a intenção E a época vigente da conta na MESMA transação e exige:

    - intenção existente, estado `SENDING`, owner correto e lease válido pelo
      relógio obtido DEPOIS da espera pela lock;
    - `dispatch_id` já registrado (o id EFETIVO desta tentativa);
    - `expected_token` presente, inteiro, igual à coluna da intenção;
    - essa coluna igual à geração FINANCEIRA vigente da conta;
    - validação manual da conta NÃO bloqueada e ownership do símbolo coerente.

    Erro, linha ausente, token NULL/bool/texto, identidade divergente ou lease
    vencido NEGAM. `may_dispatch` isolado não substitui este contrato, e uma
    época inexistente não é recriada aqui como se fosse autorização.
    """
    esperado = _finite_token(expected_token)
    if esperado is None:
        return {"ok": False, "reason_code": "DISPATCH_TOKEN_INVALID"}
    try:
        async with session_factory() as session:
            await acquire_risk_lock(session)
            agora = now or _now()
            row = (await session.execute(
                select(EntryIntent).where(EntryIntent.intent_key == intent_key)
                .with_for_update())).scalar_one_or_none()
            if row is None:
                await session.rollback()
                return {"ok": False, "reason_code": "INTENT_NOT_FOUND"}
            if row.state != STATE_SENDING or row.lease_owner != owner:
                await session.rollback()
                return {"ok": False, "reason_code": "LEASE_NOT_HELD",
                        "state": row.state}
            if row.lease_expires_at is None or row.lease_expires_at <= agora:
                await session.rollback()
                return {"ok": False, "reason_code": "LEASE_EXPIRED"}
            if dispatch_id is not None:
                registrados = [str(x) for x in (row.dispatch_ids or ())]
                if str(dispatch_id) not in registrados:
                    await session.rollback()
                    return {"ok": False, "reason_code": "DISPATCH_NOT_REGISTERED"}
            persistido = _finite_token(row.margin_generation)
            # Atributos capturados ANTES de qualquer rollback: depois dele o ORM
            # expira a instância e reler dispararia IO fora do greenlet.
            identity = EntryIdentity(
                account_ref=row.account_ref, exchange=row.exchange, symbol=row.symbol,
                quote=row.quote, side=row.side, position_side=row.position_side,
                timeframe=row.timeframe, playbook=row.playbook,
                playbook_version=row.playbook_version, purpose=row.purpose,
                trigger_candle_ms=int(row.trigger_candle_ms or 0))
            if persistido is None:
                await session.rollback()
                return {"ok": False, "reason_code": "DISPATCH_TOKEN_MISSING"}
            if persistido != esperado:
                await session.rollback()
                return {"ok": False, "reason_code": MARGIN_SUPERSEDED,
                        "persisted": persistido, "expected": esperado}
            # Época da CONTA lida na MESMA transação: mudança ALHEIA entre a
            # admissão e este ponto supera o token.
            from models.account_margin_epoch import AccountMarginEpoch as Epoch
            conta, corretora, mercado = _margin_identity(
                identity.account_ref, identity.exchange, "usdm_futures")
            epoca = (await session.execute(
                select(Epoch.generation, Epoch.manual_validation_blocked)
                .where(Epoch.account_scope == conta, Epoch.exchange == corretora,
                       Epoch.market == mercado))).one_or_none()
            if epoca is None:
                # Época inexistente NÃO é autorização: não se cria aqui.
                await session.rollback()
                return {"ok": False, "reason_code": "MARGIN_EPOCH_MISSING"}
            geracao_conta, conta_bloqueada = int(epoca[0] or 0), bool(epoca[1])
            if geracao_conta != persistido:
                await session.rollback()
                return {"ok": False, "reason_code": MARGIN_SUPERSEDED,
                        "account_generation": geracao_conta, "expected": esperado}
            if conta_bloqueada:
                await session.rollback()
                return {"ok": False, "reason_code": "MANUAL_ACCOUNT_VALIDATION_BLOCKED"}
            negado = await _ownership_denial(session, identity, "dispatch")
            await session.rollback()        # leitura decisória: nada a gravar
            if negado:
                return {"ok": False, "reason_code": negado}
            return {"ok": True, "reason_code": "DISPATCH_AUTHORIZED",
                    "token": persistido}
    except Exception as exc:  # noqa: BLE001 — dúvida NÃO autoriza
        log.warning(f"[p03-intent] autorização final indisponível: {type(exc).__name__}: {exc}")
        return {"ok": False, "reason_code": "DISPATCH_CHECK_UNAVAILABLE"}


async def mark_sending(session_factory, intent_key: str, *, owner: str,
                       lease_seconds: int = DEFAULT_LEASE_SECONDS,
                       now: Optional[datetime] = None) -> bool:
    """CAS RESERVED→SENDING. Precisa estar COMMITADO antes do primeiro POST."""
    moment = now or _now()
    deadline = moment + timedelta(seconds=max(1, int(lease_seconds)))
    try:
        async with session_factory() as session:
            await acquire_risk_lock(session)
            result = await session.execute(
                update(EntryIntent)
                .where(EntryIntent.intent_key == intent_key,
                       EntryIntent.state == STATE_RESERVED,
                       EntryIntent.lease_owner == owner)
                .values(state=STATE_SENDING, lease_expires_at=deadline, updated_at=moment,
                        dispatches=EntryIntent.dispatches + 1))
            await session.commit()
            return result.rowcount == 1
    except Exception:
        return False


async def may_dispatch(session_factory, intent_key: str, *, owner: str,
                       now: Optional[datetime] = None) -> bool:
    """Guard imediatamente antes de CADA POST/retry: só o dono do lease vivo
    em SENDING pode despachar. Dúvida ⇒ False (nenhum segundo dispatcher)."""
    moment = now or _now()
    try:
        async with session_factory() as session:
            row = (await session.execute(
                select(EntryIntent).where(EntryIntent.intent_key == intent_key)
            )).scalar_one_or_none()
            if row is None or row.state != STATE_SENDING or row.lease_owner != owner:
                return False
            return bool(row.lease_expires_at is not None and row.lease_expires_at > moment)
    except Exception:
        return False


async def _resolve(session_factory, intent_key: str, *, owner: Optional[str], state: str,
                   reason: Optional[str], real_trade_id: Optional[int] = None,
                   now: Optional[datetime] = None, require_owner: bool = True) -> bool:
    moment = now or _now()
    values = {"state": state, "reason": (_label(reason, 64) or None), "updated_at": moment,
              "lease_owner": None, "lease_expires_at": None,
              "resolved_at": moment if state in (STATE_CONFIRMED, STATE_TERMINAL) else None}
    if real_trade_id is not None:
        values["real_trade_id"] = int(real_trade_id)
    try:
        async with session_factory() as session:
            # Ordem ÚNICA: advisory lock ANTES de qualquer leitura decisória.
            await acquire_risk_lock(session)
            # Identidade lida ANTES do update: a geração de margem precisa ser
            # incrementada na MESMA transação da mudança econômica.
            atual = (await session.execute(
                select(EntryIntent.account_ref, EntryIntent.exchange)
                .where(EntryIntent.intent_key == intent_key))).one_or_none()
            if atual is not None and state in (STATE_CONFIRMED, STATE_TERMINAL):
                # Época ANTES da linha de intenção (passo 3 da ordem).
                await _lock_epoch_row(session, account_ref=atual[0],
                                      exchange=atual[1], market="usdm_futures")
            conditions = [EntryIntent.intent_key == intent_key]
            if require_owner and owner is not None:
                conditions.append(EntryIntent.lease_owner == owner)
            # Vínculo idempotente: um fill confirmado NUNCA é sobrescrito — nem
            # por uma reconfirmação, nem por um callback atrasado que chegaria
            # para rebaixá-lo a UNKNOWN/TERMINAL.
            conditions.append(EntryIntent.state != STATE_CONFIRMED)
            if state in (STATE_UNKNOWN, STATE_TERMINAL):
                # Encerrar tentativa ALHEIA em curso é proibido: só o dono do
                # lease vivo (ou um lease livre/vencido) fecha a decisão.
                conditions.append(or_(
                    EntryIntent.lease_owner.is_(None),
                    EntryIntent.lease_expires_at.is_(None),
                    EntryIntent.lease_expires_at <= moment,
                    *( [EntryIntent.lease_owner == owner] if owner is not None else [] )))
            result = await session.execute(update(EntryIntent).where(*conditions).values(**values))
            if result.rowcount == 1 and atual is not None \
                    and state in (STATE_CONFIRMED, STATE_TERMINAL):
                # CONFIRMED (com RealTrade) e TERMINAL TIRAM a intenção da soma
                # de margem pendente: toda carteira lida antes disso fica
                # obsoleta. UNKNOWN continua pendente e não muda a soma.
                await _bump_margin_generation(session, account_ref=atual[0],
                                              exchange=atual[1],
                                              market="usdm_futures")
            await session.commit()
            return result.rowcount == 1
    except Exception:
        return False


async def mark_confirmed(session_factory, intent_key: str, *, owner: Optional[str] = None,
                         real_trade_id: Optional[int] = None, reason: str = "FILL_CONFIRMED",
                         now: Optional[datetime] = None) -> bool:
    return await _resolve(session_factory, intent_key, owner=owner, state=STATE_CONFIRMED,
                          reason=reason, real_trade_id=real_trade_id, now=now, require_owner=False)


async def mark_unknown(session_factory, intent_key: str, *, owner: Optional[str] = None,
                       reason: str = "DISPATCH_OUTCOME_UNKNOWN",
                       now: Optional[datetime] = None) -> bool:
    """Desfecho incerto: exige reconciliação P03 pelo mesmo client id.

    Não rebaixa confirmação nem encerra tentativa viva de outro dono.
    """
    return await _resolve(session_factory, intent_key, owner=owner, state=STATE_UNKNOWN,
                          reason=reason, now=now, require_owner=False)


async def mark_terminal(session_factory, intent_key: str, *, owner: Optional[str] = None,
                        reason: str = "NO_ECONOMIC_ENTRY",
                        now: Optional[datetime] = None) -> bool:
    """Encerra a decisão SEM entrada econômica (não preenchida, recusada...).

    Não rebaixa confirmação nem encerra tentativa viva de outro dono.
    """
    return await _resolve(session_factory, intent_key, owner=owner, state=STATE_TERMINAL,
                          reason=reason, now=now, require_owner=False)


async def release_reserved(session_factory, intent_key: str, *, owner: str,
                           reason: str = "NOT_DISPATCHED", now: Optional[datetime] = None) -> bool:
    """Abandona uma reserva que NUNCA despachou (estado RESERVED apenas)."""
    moment = now or _now()
    try:
        async with session_factory() as session:
            await acquire_risk_lock(session)
            atual = (await session.execute(
                select(EntryIntent.account_ref, EntryIntent.exchange)
                .where(EntryIntent.intent_key == intent_key))).one_or_none()
            if atual is not None:
                await _lock_epoch_row(session, account_ref=atual[0],
                                      exchange=atual[1], market="usdm_futures")
            result = await session.execute(
                update(EntryIntent)
                .where(EntryIntent.intent_key == intent_key, EntryIntent.state == STATE_RESERVED,
                       EntryIntent.lease_owner == owner, EntryIntent.dispatches == 0)
                .values(state=STATE_TERMINAL, reason=(_label(reason, 64) or None),
                        lease_owner=None, lease_expires_at=None,
                        updated_at=moment, resolved_at=moment))
            if result.rowcount == 1 and atual is not None:
                # Liberar a reserva devolve margem: carteiras anteriores ficam
                # obsoletas (a soma de pendentes mudou).
                await _bump_margin_generation(session, account_ref=atual[0],
                                              exchange=atual[1],
                                              market="usdm_futures")
            await session.commit()
            return result.rowcount == 1
    except Exception:
        return False


async def register_dispatch(session_factory, intent_key: str, *, owner: str,
                            dispatch_id: str, now: Optional[datetime] = None) -> bool:
    """Registra o ID EFETIVO que está prestes a ser despachado.

    Roda ANTES do POST e sob o mesmo fencing do guard: só o dono do lease vivo
    em `SENDING` registra. Idempotente — o mesmo id não duplica (retry do mesmo
    envio não vira um despacho novo).
    """
    moment = now or _now()
    identifier = _label(dispatch_id, MAX_CLIENT_ORDER_ID)
    if identifier is None:
        return False
    try:
        async with session_factory() as session:
            await acquire_risk_lock(session)
            row = (await session.execute(
                select(EntryIntent).where(EntryIntent.intent_key == intent_key)
                .with_for_update())).scalar_one_or_none()
            if row is None or row.state != STATE_SENDING or row.lease_owner != owner:
                await session.rollback()
                return False
            if row.lease_expires_at is None or row.lease_expires_at <= moment:
                await session.rollback()
                return False
            current = list(row.dispatch_ids or [])
            if identifier not in current:
                current.append(identifier)
                row.dispatch_ids = current
                row.updated_at = moment
            await session.commit()
            return True
    except Exception:
        return False


async def dispatch_ids(session_factory, intent_key: str) -> list:
    row = await get_intent(session_factory, intent_key)
    return list(getattr(row, "dispatch_ids", None) or []) if row is not None else []


async def recover_stale(session_factory, *, now: Optional[datetime] = None, limit: int = 100) -> dict:
    """Boot/manutenção: lease vencido em SENDING vira UNKNOWN (nunca reenvio).

    Intenções pendentes NÃO são apagadas e TTL não presume ordem inexistente.
    """
    moment = now or _now()
    summary = {"to_unknown": 0, "reserved_released": 0}
    try:
        async with session_factory() as session:
            await acquire_risk_lock(session)
            rows = (await session.execute(
                select(EntryIntent).where(EntryIntent.state.in_((STATE_SENDING, STATE_RESERVED)),
                                          EntryIntent.lease_expires_at.is_not(None),
                                          EntryIntent.lease_expires_at <= moment)
                .order_by(EntryIntent.intent_key).limit(limit)
            )).scalars().all()
            for row in rows:
                if row.state == STATE_SENDING:
                    row.state, row.reason = STATE_UNKNOWN, "LEASE_EXPIRED_AFTER_DISPATCH"
                    summary["to_unknown"] += 1
                else:
                    # Reserva sem despacho: libera o lease, mantém a intenção.
                    summary["reserved_released"] += 1
                row.lease_owner, row.lease_expires_at = None, None
                row.updated_at = moment
            # Recovery NÃO muda a soma de margem pendente: SENDING e UNKNOWN são
            # ambos pendentes e a reserva RESERVED continua contando (só o lease
            # cai). Operação idempotente sem mudança econômica não incrementa a
            # geração — evitar crescimento/retentativa sem fim é parte do
            # contrato.
            await session.commit()
    except Exception:
        summary["error"] = "DB_UNAVAILABLE"
    return summary


#: Motivos de UNKNOWN que EXIGEM reconciliação pelo MESMO client id: a ordem
#: pode existir na exchange. `PENDING_ENTRY_ORDER` NÃO entra — ali a ordem é
#: conhecida e viva, não um desfecho incerto.
RECONCILE_REASONS = ("DISPATCH_OUTCOME_UNKNOWN", "SAFETY_STATE_UNKNOWN",
                     "PERSISTENCE_FAILED", "LEASE_EXPIRED_AFTER_DISPATCH")


async def list_needing_reconciliation(session_factory, *, limit: int = 50) -> list:
    """Intenções cujo desfecho de ENVIO continua incerto e sem RealTrade.

    Devolve dicionários simples (a sessão fecha antes do uso): quem consome é o
    ciclo de reconciliação, que trabalha pelo `client_order_id` e pela conta.
    """
    try:
        async with session_factory() as session:
            rows = (await session.execute(
                select(EntryIntent)
                .where(EntryIntent.state == STATE_UNKNOWN,
                       EntryIntent.real_trade_id.is_(None),
                       EntryIntent.reason.in_(RECONCILE_REASONS))
                .order_by(EntryIntent.updated_at).limit(limit))).scalars().all()
            return [{"intent_key": row.intent_key, "client_order_id": row.client_order_id,
                     "account_ref": row.account_ref, "exchange": row.exchange,
                     "symbol": row.symbol, "side": row.side, "reason": row.reason,
                     # TODOS os ids efetivamente despachados (inclui a filha
                     # `-mfb`): primária rejeitada não prova ausência de fill.
                     "dispatch_ids": list(row.dispatch_ids or []),
                     # Stop/qty da decisão: sem eles a recuperação não consegue
                     # nem ADOTAR um SL vivo e válido já existente.
                     "decision_payload": dict(row.decision_payload or {}) or None,
                     "updated_at": row.updated_at} for row in rows]
    except Exception:
        return []


async def get_intent(session_factory, intent_key: str):
    try:
        async with session_factory() as session:
            return (await session.execute(
                select(EntryIntent).where(EntryIntent.intent_key == intent_key)
            )).scalar_one_or_none()
    except Exception:
        return None
