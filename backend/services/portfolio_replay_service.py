"""R10D — replay de CARTEIRA compartilhada sobre o motor R10A existente.

Não é um segundo sistema de backtest: a trajetória de cada trade continua sendo
resolvida por `offline_replay_service.replay_opportunity` (R10A), identificável
e intocado. Aqui entram as camadas que faltavam:

  • universo/config/velas disponíveis NO INSTANTE da decisão — o universo de
    hoje nunca substitui o ponto-no-tempo;
  • latência de scan/envio, preço executável e gate revalidado nesse instante;
  • maker não preenchido, parcial, cancelamento e fallback SOMENTE quando a
    configuração simulada permite; o default não liga recurso desligado;
  • carteira compartilhada: capital, exposição, reservas, slots, simultaneidade
    e ordenação determinística — trades impossíveis não são somados;
  • custos separados (taxa por fill, funding por evento, slippage coerente com a
    liquidez); ausência ≠ zero;
  • matriz de fidelidade por dimensão: comprovado / modelado / indisponível.

OHLCV não prova fila, fill nem liquidez histórica. `live_equivalent` é sempre
`False`: compartilhar função não transforma cenário em execução observada.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
import hashlib
import json
import math
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from services import offline_replay_service as r10a

PORTFOLIO_VERSION = "R10D_PORTFOLIO_REPLAY_V1"
CONTRACT = "CANDIDATE_POLICY"
MODE_ENV = "R10_PORTFOLIO_REPLAY_MODE"
MODE_INACTIVE = "inactive"
MODE_SIMULATION = "simulation"

SIDE_LONG = "long"
SIDE_SHORT = "short"

# ── Vocabulário de motivos ──────────────────────────────────────────────────
OK = "OK"
UNIVERSE_UNKNOWN = "UNIVERSE_UNKNOWN"
SYMBOL_OUT_OF_UNIVERSE = "SYMBOL_OUT_OF_UNIVERSE"
QUOTE_UNAVAILABLE = "QUOTE_UNAVAILABLE"
QUOTE_BEFORE_DECISION = "QUOTE_BEFORE_DECISION"
QUOTE_STALE = "QUOTE_STALE"
SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
CHASE_TOO_FAR = "CHASE_TOO_FAR"
ATR_UNKNOWN = "ATR_UNKNOWN"
MAKER_NOT_FILLED = "MAKER_NOT_FILLED"
PARTIAL_NOT_ALLOWED = "PARTIAL_NOT_ALLOWED"
FALLBACK_NOT_ALLOWED = "FALLBACK_NOT_ALLOWED"
NO_SLOT = "NO_SLOT"
SYMBOL_SLOT_TAKEN = "SYMBOL_SLOT_TAKEN"
NO_CAPITAL = "NO_CAPITAL"
EXPOSURE_LIMIT = "EXPOSURE_LIMIT"
BARS_UNAVAILABLE = "BARS_UNAVAILABLE"
GEOMETRY_INVALID_AFTER_FILL = "GEOMETRY_INVALID_AFTER_FILL"
COST_COMPONENT_UNKNOWN = "COST_COMPONENT_UNKNOWN"
ECONOMICS_UNAVAILABLE = "ECONOMICS_UNAVAILABLE"
SAME_BAR_CONSERVATIVE_STOP = "SAME_BAR_CONSERVATIVE_STOP"
GAP_FILL_AT_OPEN = "GAP_FILL_AT_OPEN"
BAR_INCOMPLETE = "BAR_INCOMPLETE"
REPLAY_INACTIVE = "REPLAY_INACTIVE"
REASON_CODES = frozenset({
    OK, UNIVERSE_UNKNOWN, SYMBOL_OUT_OF_UNIVERSE, QUOTE_UNAVAILABLE,
    QUOTE_BEFORE_DECISION, QUOTE_STALE, SPREAD_TOO_WIDE, CHASE_TOO_FAR, ATR_UNKNOWN,
    MAKER_NOT_FILLED, PARTIAL_NOT_ALLOWED, FALLBACK_NOT_ALLOWED, NO_SLOT,
    SYMBOL_SLOT_TAKEN, NO_CAPITAL, EXPOSURE_LIMIT, BARS_UNAVAILABLE,
    GEOMETRY_INVALID_AFTER_FILL,
    COST_COMPONENT_UNKNOWN, ECONOMICS_UNAVAILABLE, SAME_BAR_CONSERVATIVE_STOP,
    GAP_FILL_AT_OPEN, BAR_INCOMPLETE, REPLAY_INACTIVE,
})

# ── Matriz de fidelidade ────────────────────────────────────────────────────
FIDELITY_PROVEN = "PROVEN"
FIDELITY_MODELED = "MODELED"
FIDELITY_UNAVAILABLE = "UNAVAILABLE"
FIDELITY_DIMENSIONS = ("point_in_time_universe", "decision_rule", "price_path",
                       "entry_fill", "queue_position", "partial_fills", "latency",
                       "fees", "funding", "slippage_liquidity", "portfolio_limits",
                       "exchange_rejections")

LIMITATIONS = [
    "Cenário de pesquisa: não é execução observada nem prova de rentabilidade.",
    "OHLCV não prova fila, fill parcial ou liquidez histórica.",
    "Custo ausente deixa a economia indisponível — nunca zero.",
    "Stop e alvo na mesma barra seguem a regra conservadora declarada.",
    "Trades impossíveis pela carteira não entram na soma.",
]


def selected_mode() -> str:
    value = (os.getenv(MODE_ENV, MODE_INACTIVE) or "").strip().lower()
    return MODE_SIMULATION if value == MODE_SIMULATION else MODE_INACTIVE


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False, default=str).encode("utf-8")).hexdigest()


# ── Universo ponto-no-tempo ─────────────────────────────────────────────────
@dataclass(frozen=True)
class UniverseSnapshot:
    as_of_ms: int
    symbols: Tuple[str, ...]

    def __post_init__(self) -> None:
        if _int(self.as_of_ms) is None or self.as_of_ms < 0:
            raise ValueError("as_of_ms inválido")
        if not isinstance(self.symbols, tuple) or any(not isinstance(s, str) or not s
                                                      for s in self.symbols):
            raise ValueError("symbols: tupla de símbolos não vazios")


@dataclass(frozen=True)
class PointInTimeUniverse:
    snapshots: Tuple[UniverseSnapshot, ...] = ()

    def at(self, timestamp_ms: Any) -> Dict[str, Any]:
        """Universo vigente no instante. Sem snapshot anterior, UNKNOWN."""
        moment = _int(timestamp_ms)
        if moment is None:
            return {"symbols": (), "as_of_ms": None, "reason_code": UNIVERSE_UNKNOWN}
        eligible = [snap for snap in self.snapshots if snap.as_of_ms <= moment]
        if not eligible:
            # O universo ATUAL não substitui o ponto-no-tempo.
            return {"symbols": (), "as_of_ms": None, "reason_code": UNIVERSE_UNKNOWN}
        chosen = max(eligible, key=lambda snap: snap.as_of_ms)
        return {"symbols": chosen.symbols, "as_of_ms": chosen.as_of_ms, "reason_code": OK}

    def admits(self, symbol: Any, timestamp_ms: Any) -> Dict[str, Any]:
        view = self.at(timestamp_ms)
        if view["reason_code"] != OK:
            return {"admitted": False, **view}
        admitted = isinstance(symbol, str) and symbol in view["symbols"]
        return {"admitted": admitted,
                "reason_code": OK if admitted else SYMBOL_OUT_OF_UNIVERSE, **{
                    "symbols": view["symbols"], "as_of_ms": view["as_of_ms"]}}


# ── Modelo de execução ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class ExecutionModel:
    scan_latency_ms: int = 0
    send_latency_ms: int = 0
    max_quote_age_ms: int = 5_000
    max_spread_pct: float = 0.10
    max_chase_atr: float = 0.5
    revalidate_gate: bool = True
    #: Recursos desligados por DEFAULT: nada liga sozinho no replay.
    maker_enabled: bool = False
    allow_partial_fill: bool = False
    allow_cancel_fallback: bool = False

    def __post_init__(self) -> None:
        for name in ("scan_latency_ms", "send_latency_ms", "max_quote_age_ms"):
            value = _int(getattr(self, name))
            if value is None or value < 0:
                raise ValueError(f"{name}: inteiro não negativo obrigatório")
        for name in ("max_spread_pct", "max_chase_atr"):
            value = _finite(getattr(self, name))
            if value is None or value < 0:
                raise ValueError(f"{name}: número não negativo obrigatório")
        for name in ("revalidate_gate", "maker_enabled", "allow_partial_fill",
                     "allow_cancel_fallback"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name}: booleano obrigatório")

    @property
    def total_latency_ms(self) -> int:
        return self.scan_latency_ms + self.send_latency_ms

    def as_dict(self) -> Dict[str, Any]:
        return {spec.name: getattr(self, spec.name) for spec in fields(self)}


def executable_price(quote: Any, *, side: str, decision_ts_ms: int,
                     planned_entry: Any, atr: Any,
                     model: ExecutionModel) -> Dict[str, Any]:
    """Preço executável NO instante decisão+latência, com gate revalidado."""
    effective_ms = decision_ts_ms + model.total_latency_ms
    base = {"price": None, "fill_type": "NONE", "effective_ts_ms": effective_ms,
            "checks": {}}
    if not isinstance(quote, Mapping):
        return {**base, "reason_code": QUOTE_UNAVAILABLE}
    bid, ask = _finite(quote.get("bid")), _finite(quote.get("ask"))
    stamp = _int(quote.get("ts_ms"))
    if bid is None or ask is None or stamp is None or bid <= 0 or ask <= 0 or ask < bid:
        return {**base, "reason_code": QUOTE_UNAVAILABLE}
    if stamp < effective_ms:
        # Cotação anterior ao instante executável seria vantagem impossível.
        return {**base, "reason_code": QUOTE_BEFORE_DECISION}
    if stamp - effective_ms > model.max_quote_age_ms:
        return {**base, "reason_code": QUOTE_STALE}
    mid = (bid + ask) / 2.0
    spread_pct = (ask - bid) / mid * 100.0 if mid > 0 else float("inf")
    checks = {"spread_pct": spread_pct, "bid": bid, "ask": ask,
              "quote_ts_ms": stamp, "source": quote.get("source")}
    price = ask if side == SIDE_LONG else bid
    fill_type = "TAKER"
    if model.maker_enabled:
        price = bid if side == SIDE_LONG else ask
        fill_type = "MAKER"
    if model.revalidate_gate:
        if spread_pct > model.max_spread_pct:
            return {**base, "checks": checks, "reason_code": SPREAD_TOO_WIDE}
        planned = _finite(planned_entry)
        atr_value = _finite(atr)
        if planned is None:
            return {**base, "checks": checks, "reason_code": QUOTE_UNAVAILABLE}
        if model.max_chase_atr > 0:
            if atr_value is None or atr_value <= 0:
                return {**base, "checks": checks, "reason_code": ATR_UNKNOWN}
            adverse = (price - planned) if side == SIDE_LONG else (planned - price)
            checks["chase_atr"] = adverse / atr_value
            if adverse / atr_value > model.max_chase_atr:
                return {**base, "checks": checks, "reason_code": CHASE_TOO_FAR}
    return {"price": price, "fill_type": fill_type, "effective_ts_ms": effective_ms,
            "checks": checks, "reason_code": OK}


def maker_outcome(*, limit_price: float, side: str, bar: Any,
                  model: ExecutionModel) -> Dict[str, Any]:
    """Maker só preenche se a barra negociou através do limite.

    Parcial e fallback para taker existem apenas quando a configuração simulada
    os habilita — o default NÃO liga recurso desligado.
    """
    if not model.maker_enabled:
        return {"filled": True, "fraction": 1.0, "fill_type": "TAKER", "reason_code": OK}
    if not isinstance(bar, Mapping):
        return {"filled": False, "fraction": 0.0, "fill_type": "NONE",
                "reason_code": BAR_INCOMPLETE}
    low, high = _finite(bar.get("low")), _finite(bar.get("high"))
    if low is None or high is None:
        return {"filled": False, "fraction": 0.0, "fill_type": "NONE",
                "reason_code": BAR_INCOMPLETE}
    touched = low <= limit_price if side == SIDE_LONG else high >= limit_price
    if not touched:
        if model.allow_cancel_fallback:
            return {"filled": True, "fraction": 1.0, "fill_type": "TAKER_FALLBACK",
                    "reason_code": OK}
        return {"filled": False, "fraction": 0.0, "fill_type": "NONE",
                "reason_code": MAKER_NOT_FILLED}
    crossed = low < limit_price if side == SIDE_LONG else high > limit_price
    if not crossed and not model.allow_partial_fill:
        # Tocar o limite não prova fila atendida; sem parcial habilitado, não enche.
        return {"filled": False, "fraction": 0.0, "fill_type": "NONE",
                "reason_code": PARTIAL_NOT_ALLOWED}
    fraction = 1.0 if crossed else 0.5
    return {"filled": True, "fraction": fraction, "fill_type": "MAKER",
            "reason_code": OK}


def same_bar_rule(*, side: str, bar: Any, stop: float, target: float) -> Dict[str, Any]:
    """Stop e alvo na mesma barra, sem resolução mais fina: conservador."""
    if not isinstance(bar, Mapping):
        return {"resolution": None, "reason_code": BAR_INCOMPLETE}
    low, high, open_ = (_finite(bar.get("low")), _finite(bar.get("high")),
                        _finite(bar.get("open")))
    if low is None or high is None or open_ is None or bar.get("closed") is False:
        return {"resolution": None, "reason_code": BAR_INCOMPLETE}
    if side == SIDE_LONG:
        stop_hit, target_hit, gapped = low <= stop, high >= target, open_ <= stop
    else:
        stop_hit, target_hit, gapped = high >= stop, low <= target, open_ >= stop
    if gapped:
        return {"resolution": "STOP", "fill_price": open_, "reason_code": GAP_FILL_AT_OPEN}
    if stop_hit and target_hit:
        return {"resolution": "STOP", "fill_price": stop,
                "reason_code": SAME_BAR_CONSERVATIVE_STOP}
    if stop_hit:
        return {"resolution": "STOP", "fill_price": stop, "reason_code": OK}
    if target_hit:
        return {"resolution": "TARGET", "fill_price": target, "reason_code": OK}
    return {"resolution": None, "reason_code": OK}


# ── Carteira compartilhada ──────────────────────────────────────────────────
@dataclass(frozen=True)
class PortfolioConfig:
    capital_usd: float = 1_000.0
    risk_per_trade_pct: float = 1.0
    max_concurrent: int = 3
    max_per_symbol: int = 1
    max_exposure_usd: float = 3_000.0
    reserve_usd: float = 0.0

    def __post_init__(self) -> None:
        for name in ("capital_usd", "risk_per_trade_pct", "max_exposure_usd", "reserve_usd"):
            value = _finite(getattr(self, name))
            if value is None or value < 0:
                raise ValueError(f"{name}: número não negativo obrigatório")
        for name in ("max_concurrent", "max_per_symbol"):
            value = _int(getattr(self, name))
            if value is None or value < 1:
                raise ValueError(f"{name}: inteiro positivo obrigatório")

    @property
    def risk_usd(self) -> float:
        return self.capital_usd * self.risk_per_trade_pct / 100.0

    def as_dict(self) -> Dict[str, Any]:
        return {spec.name: getattr(self, spec.name) for spec in fields(self)}


@dataclass
class PortfolioState:
    config: PortfolioConfig
    open_positions: List[Dict[str, Any]] = field(default_factory=list)
    used_risk_usd: float = 0.0
    exposure_usd: float = 0.0
    #: Capital CORRENTE: recebe o resultado realizado de cada trade fechado.
    #: Sem isto, duas perdas sequenciais arriscariam o capital inicial duas
    #: vezes e a carteira perderia mais do que tem.
    capital_usd: Optional[float] = None
    realized_pnl_usd: float = 0.0
    unknown_results: int = 0
    #: Capital observado no instante da ENTRADA e no instante da SAÍDA de cada
    #: trade — a prova de que o sizing não usou resultado que ainda não ocorreu.
    capital_at_entry: Dict[str, float] = field(default_factory=dict)
    capital_at_exit: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.capital_usd is None:
            self.capital_usd = float(self.config.capital_usd)

    @property
    def available_capital_usd(self) -> float:
        return max(0.0, float(self.capital_usd) - float(self.config.reserve_usd))

    def risk_budget_usd(self) -> float:
        """Risco por trade sobre o capital CORRENTE, nunca o inicial."""
        return max(0.0, float(self.capital_usd)) * self.config.risk_per_trade_pct / 100.0

    def schedule_settlement(self, *, key: str, risk_usd: float,
                            net_r: Optional[float]) -> None:
        """Guarda o resultado A LIQUIDAR da posição aberta `key`.

        O resultado existe na simulação assim que a trajetória é calculada, mas
        só pode tocar o capital no instante COMPROVADO da saída: aplicá-lo agora
        financiaria a próxima entrada com dinheiro do futuro.
        """
        for position in self.open_positions:
            if position["key"] == key:
                position["settle_risk_usd"] = float(risk_usd)
                position["net_r"] = _finite(net_r)
                position["result_known"] = _finite(net_r) is not None
                return

    def settle(self, *, risk_usd: float, net_r: Optional[float],
               key: Optional[str] = None) -> None:
        """Aplica o resultado realizado ao capital, NO instante da saída.

        Resultado desconhecido NÃO libera capital presumido nem zera prejuízo:
        fica contado como desconhecido e o risco segue reservado.
        """
        value = _finite(net_r)
        if value is None:
            self.unknown_results += 1
            if key is not None:
                self.capital_at_exit[key] = float(self.capital_usd)
            return
        realized = value * float(risk_usd)
        self.realized_pnl_usd += realized
        self.capital_usd = float(self.capital_usd) + realized
        if key is not None:
            self.capital_at_exit[key] = float(self.capital_usd)

    def release(self, now_ms: Optional[int]) -> None:
        """Fecha — e LIQUIDA — posições cujo horizonte terminou até `now_ms`.

        Ordem temporal determinística (saída, depois identidade): o capital de
        uma nova decisão é o que existia no instante dela, nem um dólar do que
        ainda vai acontecer. `now_ms=None` drena tudo (fim do replay).
        """
        fechando, ainda_abertas = [], []
        for position in self.open_positions:
            saida = position["exit_ts_ms"]
            encerrada = saida is not None and (now_ms is None or saida <= now_ms)
            (fechando if encerrada else ainda_abertas).append(position)
        self.open_positions = ainda_abertas
        for position in sorted(fechando, key=lambda p: (p["exit_ts_ms"], p["key"])):
            self.exposure_usd -= position["exposure_usd"]
            conhecido = bool(position.get("result_known"))
            if conhecido:
                # Resultado provado: o risco reservado volta e o P&L entra.
                self.used_risk_usd -= position["risk_usd"]
            self.settle(risk_usd=position.get("settle_risk_usd", position["risk_usd"]),
                        net_r=position.get("net_r"), key=position["key"])
            self.used_risk_usd = max(0.0, self.used_risk_usd)
            self.exposure_usd = max(0.0, self.exposure_usd)
        self.used_risk_usd = max(0.0, self.used_risk_usd)
        self.exposure_usd = max(0.0, self.exposure_usd)

    def admit(self, *, key: str, symbol: str, side: str, decision_ts_ms: int,
              exposure_usd: float, exit_ts_ms: Optional[int],
              event_ts_ms: Optional[int] = None) -> Dict[str, Any]:
        cfg = self.config
        self.release(decision_ts_ms)
        same_symbol = sum(1 for p in self.open_positions if p["symbol"] == symbol)
        if same_symbol >= cfg.max_per_symbol:
            return {"admitted": False, "reason_code": SYMBOL_SLOT_TAKEN}
        if len(self.open_positions) >= cfg.max_concurrent:
            return {"admitted": False, "reason_code": NO_SLOT}
        risk = self.risk_budget_usd()
        if risk <= 0 or self.used_risk_usd + risk > self.available_capital_usd + 1e-9:
            return {"admitted": False, "reason_code": NO_CAPITAL}
        if self.exposure_usd + exposure_usd > cfg.max_exposure_usd + 1e-9:
            return {"admitted": False, "reason_code": EXPOSURE_LIMIT}
        self.open_positions.append({"key": key, "symbol": symbol, "side": side,
                                    "risk_usd": risk, "exposure_usd": exposure_usd,
                                    "entry_ts_ms": decision_ts_ms,
                                    # `exit_ts_ms` aqui é o instante de
                                    # DISPONIBILIDADE; o evento fica à parte.
                                    "exit_ts_ms": exit_ts_ms,
                                    "event_ts_ms": event_ts_ms,
                                    "settle_risk_usd": risk, "net_r": None,
                                    "result_known": False})
        self.used_risk_usd += risk
        self.exposure_usd += exposure_usd
        self.capital_at_entry[key] = float(self.capital_usd)
        return {"admitted": True, "reason_code": OK, "risk_usd": risk}


def order_candidates(candidates: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """Ordenação determinística: instante da decisão, depois identidade."""
    return sorted(candidates or (),
                  key=lambda item: (_int(item.get("decision_ts_ms")) or 0,
                                    str(item.get("opportunity_id") or "")))


# ── Custos ──────────────────────────────────────────────────────────────────
def cost_status(costs: r10a.CostConfig) -> Dict[str, Any]:
    manifest = costs.manifest()
    missing = [name for name, value in
               (("fee_bps_per_side", costs.fee_bps_per_side),
                ("slippage_bps_per_side", costs.slippage_bps_per_side),
                ("funding_bps_per_bar", costs.funding_bps_per_bar)) if value is None]
    return {"status": "DECLARED_SCENARIO" if manifest["complete"] else "UNKNOWN",
            "complete": manifest["complete"], "missing": missing,
            "observed_account_costs": False,
            "reason_code": OK if manifest["complete"] else COST_COMPONENT_UNKNOWN}


# ── Matriz de fidelidade ────────────────────────────────────────────────────
def fidelity_matrix(*, universe: PointInTimeUniverse, model: ExecutionModel,
                    costs: r10a.CostConfig, quotes_observed: bool) -> Dict[str, Any]:
    complete = costs.manifest()["complete"]
    matrix = {
        "point_in_time_universe": (FIDELITY_PROVEN if universe.snapshots
                                   else FIDELITY_UNAVAILABLE),
        "decision_rule": FIDELITY_PROVEN,
        "price_path": FIDELITY_MODELED,
        "entry_fill": FIDELITY_MODELED if quotes_observed else FIDELITY_UNAVAILABLE,
        "queue_position": FIDELITY_UNAVAILABLE,
        "partial_fills": (FIDELITY_MODELED if model.allow_partial_fill
                          else FIDELITY_UNAVAILABLE),
        "latency": FIDELITY_MODELED,
        "fees": FIDELITY_MODELED if complete else FIDELITY_UNAVAILABLE,
        "funding": (FIDELITY_MODELED if costs.funding_bps_per_bar is not None
                    else FIDELITY_UNAVAILABLE),
        "slippage_liquidity": (FIDELITY_MODELED if costs.slippage_bps_per_side is not None
                               else FIDELITY_UNAVAILABLE),
        "portfolio_limits": FIDELITY_MODELED,
        "exchange_rejections": FIDELITY_UNAVAILABLE,
    }
    return {"dimensions": matrix, "live_equivalent": False,
            "proven": sorted(k for k, v in matrix.items() if v == FIDELITY_PROVEN),
            "unavailable": sorted(k for k, v in matrix.items() if v == FIDELITY_UNAVAILABLE)}


# ── Execução da carteira ────────────────────────────────────────────────────
def run_portfolio(candidates: Sequence[Mapping[str, Any]], *,
                  bars_by_id: Mapping[str, Sequence[Mapping[str, Any]]],
                  quotes_by_id: Optional[Mapping[str, Mapping[str, Any]]] = None,
                  universe: Optional[PointInTimeUniverse] = None,
                  model: Optional[ExecutionModel] = None,
                  portfolio: Optional[PortfolioConfig] = None,
                  replay_config: Optional[r10a.ReplayConfig] = None,
                  costs: Optional[r10a.CostConfig] = None) -> Dict[str, Any]:
    """Roda a política inteira sobre a MESMA carteira, em ordem determinística."""
    universe = universe or PointInTimeUniverse()
    model = model or ExecutionModel()
    portfolio = portfolio or PortfolioConfig()
    replay_config = replay_config or r10a.ReplayConfig()
    costs = costs or r10a.CostConfig()
    state = PortfolioState(config=portfolio)
    quotes_by_id = quotes_by_id or {}
    rejected: Dict[str, int] = {}
    trades: List[Dict[str, Any]] = []
    net_values: List[float] = []
    economics_unavailable = 0

    def reject(key: str, reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1
        trades.append({"opportunity_id": key, "admitted": False, "reason_code": reason,
                       "net_r": None})

    for raw in order_candidates(candidates):
        key = str(raw.get("opportunity_id") or "")
        symbol = raw.get("symbol")
        side = raw.get("direction")
        decision_ms = _int(raw.get("decision_ts_ms"))
        if decision_ms is None or side not in (SIDE_LONG, SIDE_SHORT) or not key:
            reject(key, BARS_UNAVAILABLE)
            continue
        if universe.snapshots:
            verdict = universe.admits(symbol, decision_ms)
            if not verdict["admitted"]:
                reject(key, verdict["reason_code"])
                continue
        quote = quotes_by_id.get(key)
        entry_verdict = executable_price(
            quote, side=side, decision_ts_ms=decision_ms,
            planned_entry=raw.get("entry"), atr=raw.get("atr"), model=model)
        if entry_verdict["reason_code"] != OK:
            reject(key, entry_verdict["reason_code"])
            continue
        bars = bars_by_id.get(key)
        if not bars:
            reject(key, BARS_UNAVAILABLE)
            continue
        try:
            candles = tuple(r10a.Candle(**dict(bar)) for bar in bars)
        except (TypeError, ValueError):
            reject(key, BARS_UNAVAILABLE)
            continue
        # A ENTRADA EFETIVA e o INSTANTE efetivo alimentam a trajetória: o
        # replay começa depois do fill (nunca uma saída antes dele) e o R sai do
        # preço realmente simulado, não do preço planejado.
        effective_price = _finite(entry_verdict["price"])
        effective_ts = _int(entry_verdict["effective_ts_ms"])
        if effective_price is None or effective_ts is None:
            reject(key, QUOTE_UNAVAILABLE)
            continue
        if model.maker_enabled:
            # Maker só abre quando a barra da janela realmente atravessou o
            # limite; toque sem prova de fila não vira posição.
            entry_bar = next((bar for bar in bars
                              if (_int(bar.get("timestamp_ms")) or -1) >= effective_ts), None)
            fill = maker_outcome(limit_price=effective_price, side=side,
                                 bar=entry_bar, model=model)
            if not fill["filled"]:
                reject(key, fill["reason_code"])
                continue
            entry_verdict = {**entry_verdict, "fill_type": fill["fill_type"],
                             "fill_fraction": fill["fraction"]}
        try:
            opportunity = r10a.Opportunity(
                opportunity_id=key, symbol=str(symbol), direction=side,
                decision_ts_ms=effective_ts, entry=effective_price,
                stop_loss=raw.get("stop_loss"), tp1=raw.get("tp1"), tp2=raw.get("tp2"),
                atr=raw.get("atr"))
        except (TypeError, ValueError):
            # Preço efetivo rompeu a geometria (stop/alvo) — não existe trade.
            reject(key, GEOMETRY_INVALID_AFTER_FILL)
            continue
        risk_price = abs(opportunity.entry - opportunity.stop_loss)
        if risk_price <= 0:
            reject(key, GEOMETRY_INVALID_AFTER_FILL)
            continue
        # Estado CAUSAL antes de dimensionar: tudo que já saiu até este
        # instante liquida agora; o que sai depois não financia esta entrada.
        state.release(effective_ts)
        risk_budget = state.risk_budget_usd()
        fraction = _finite(entry_verdict.get("fill_fraction"))
        fraction = 1.0 if fraction is None else max(0.0, min(1.0, fraction))
        qty = (risk_budget / risk_price) * fraction
        exposure = qty * opportunity.entry
        # A trajetória começa na PRIMEIRA barra completa depois do fill: barra
        # anterior ao instante efetivo não pode produzir saída.
        bar_ms = replay_config.bar_ms
        first_ms = ((effective_ts + bar_ms - 1) // bar_ms) * bar_ms
        window = tuple(candle for candle in candles if candle.timestamp_ms >= first_ms)
        if not window:
            reject(key, BARS_UNAVAILABLE)
            continue
        result = r10a.replay_opportunity(opportunity, window, replay_config, costs)
        # O slot e o capital só voltam quando o resultado É CONHECÍVEL — a vela
        # que produziu a saída precisa ter fechado. Usar a abertura dela
        # financiaria uma decisão anterior com preço que ainda não existia.
        disponivel = _int(result.get("result_available_ts_ms"))
        admission = state.admit(key=key, symbol=str(symbol), side=side,
                                decision_ts_ms=effective_ts, exposure_usd=exposure,
                                exit_ts_ms=disponivel,
                                event_ts_ms=_int(result.get("exit_ts_ms")))
        if not admission["admitted"]:
            # Trade impossível pela carteira NÃO entra na soma.
            reject(key, admission["reason_code"])
            continue
        net = _finite(result.get("net_r"))
        if net is None:
            economics_unavailable += 1
        else:
            net_values.append(net)
        risk_usd = admission["risk_usd"] * fraction
        # Resultado fica A LIQUIDAR: entra no capital no instante da saída.
        state.schedule_settlement(key=key, risk_usd=risk_usd, net_r=net)
        trades.append({"opportunity_id": key, "admitted": True, "reason_code": OK,
                       "status": result.get("status"), "net_r": net,
                       "gross_r": _finite(result.get("gross_r")),
                       "fee_r": _finite(result.get("fee_r")),
                       "slippage_r": _finite(result.get("slippage_r")),
                       "funding_r": _finite(result.get("funding_r")),
                       "entry_fill_type": entry_verdict["fill_type"],
                       "entry_fill_price": opportunity.entry,
                       "fill_fraction": fraction,
                       "effective_ts_ms": effective_ts,
                       "exit_ts_ms": result.get("exit_ts_ms"),
                       "result_available_ts_ms": result.get("result_available_ts_ms"),
                       "risk_usd": risk_usd, "qty": qty, "exposure_usd": exposure,
                       "capital_at_entry_usd": state.capital_at_entry.get(key)})
    # Fim do replay: o que ainda estava aberto liquida em ordem temporal.
    state.release(None)
    for row in trades:
        if row.get("admitted"):
            # Capital DEPOIS deste trade = capital no instante da SAÍDA dele.
            row["capital_after_usd"] = state.capital_at_exit.get(row["opportunity_id"])
    costs_view = cost_status(costs)
    metrics = portfolio_metrics(net_values)
    metrics = {**metrics, "capital_start_usd": portfolio.capital_usd,
               "capital_end_usd": state.capital_usd,
               "realized_pnl_usd": state.realized_pnl_usd,
               "unknown_results": state.unknown_results}
    if not costs_view["complete"]:
        metrics = {**metrics, "economics": ECONOMICS_UNAVAILABLE}
    return {
        "portfolio_version": PORTFOLIO_VERSION,
        "contract": CONTRACT,
        "mode": selected_mode(),
        "live_equivalent": False,
        "promotable": False,
        "engine": {"trade_path": r10a.SCHEMA_VERSION, "reused_r10a": True},
        "config_hash": _hash({"model": model.as_dict(), "portfolio": portfolio.as_dict(),
                              "replay": replay_config.manifest()["config_hash"],
                              "costs": costs.manifest()["config_hash"]}),
        "trades": trades,
        "admitted": sum(1 for row in trades if row["admitted"]),
        "rejected": rejected,
        "economics_unavailable": economics_unavailable,
        "metrics": metrics,
        "costs": costs_view,
        "fidelity": fidelity_matrix(universe=universe, model=model, costs=costs,
                                    quotes_observed=bool(quotes_by_id)),
        "limitations": list(LIMITATIONS),
    }


def portfolio_metrics(net_values: Sequence[float]) -> Dict[str, Any]:
    """Métricas da CARTEIRA em R. Sem amostra resolvida, tudo indisponível."""
    values = [value for value in (net_values or ()) if _finite(value) is not None]
    if not values:
        return {"resolved_n": 0, "net_total_r": None, "net_expectancy_r": None,
                "win_rate_pct": None, "profit_factor": None,
                "profit_factor_reason": "NO_RESOLVED_SAMPLE",
                "max_drawdown_r": None, "economics": ECONOMICS_UNAVAILABLE}
    wins = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {"resolved_n": len(values), "net_total_r": sum(values),
            "net_expectancy_r": sum(values) / len(values),
            "win_rate_pct": 100.0 * sum(1 for v in values if v > 0) / len(values),
            "profit_factor": (wins / losses) if losses > 0 else None,
            "profit_factor_reason": None if losses > 0 else "NO_LOSSES_DENOMINATOR",
            "max_drawdown_r": drawdown, "economics": "AVAILABLE"}


def portfolio_manifest() -> Dict[str, Any]:
    return {
        "portfolio_version": PORTFOLIO_VERSION,
        "contract": CONTRACT,
        "mode": selected_mode(),
        "live_equivalent": False,
        "promotable": False,
        "reuses": {"trade_path_engine": "offline_replay_service (R10A)",
                   "second_backtest_system": False},
        "defaults_off": ["maker_enabled", "allow_partial_fill", "allow_cancel_fallback"],
        "fidelity_dimensions": list(FIDELITY_DIMENSIONS),
        "reason_codes": sorted(REASON_CODES),
        "conservative_rules": {
            "same_bar_stop_and_target": SAME_BAR_CONSERVATIVE_STOP,
            "gap_through_stop": GAP_FILL_AT_OPEN,
            "incomplete_bar": BAR_INCOMPLETE,
        },
        "limitations": list(LIMITATIONS),
    }
