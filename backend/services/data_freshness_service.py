"""P04C — contrato puro de validade dos dados usados antes da entrada live.

O módulo não busca rede, não altera score e não envia ordens. Ele apenas:

* remove a vela corrente ainda aberta antes de calcular o sinal;
* rejeita séries inválidas ou cujo último candle fechado ficou vencido;
* valida, no último gate live, a identidade/idade do candle e dos contextos
  opcionais que efetivamente participaram da recomendação.

Preço executável e profundidade continuam sob P04A/P04B, imediatamente antes
do POST da ordem. P04C protege os dados que formaram o plano.
"""
from __future__ import annotations

import math
import time
from typing import Any, Optional


_TIMEFRAME_MS = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "2h": 2 * 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "6h": 6 * 60 * 60_000,
    "8h": 8 * 60 * 60_000,
    "12h": 12 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
    "3d": 3 * 24 * 60 * 60_000,
    "1w": 7 * 24 * 60 * 60_000,
    # Aproximação usada apenas para freshness. O calendário do provedor ainda
    # é a fonte da abertura; 31 dias deixam a validação conservadora sem
    # inventar um fechamento antecipado.
    "1M": 31 * 24 * 60 * 60_000,
}


def timeframe_ms(timeframe: Any) -> Optional[int]:
    """Converte um timeframe conhecido em milissegundos; desconhecido = None."""
    if not isinstance(timeframe, str):
        return None
    value = timeframe.strip()
    if value == "1M":
        return _TIMEFRAME_MS[value]
    return _TIMEFRAME_MS.get(value.lower())


def _now_ms() -> int:
    return int(time.time() * 1000)


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _timestamp_ms(value: Any) -> Optional[int]:
    number = _number(value)
    if number is None or number <= 0:
        return None
    # Aceita epoch em segundos somente no intervalo plausível de datas reais.
    # Valores pequenos são úteis em testes puros e permanecem em ms.
    if 1_000_000_000 <= number < 100_000_000_000:
        number *= 1000
    try:
        return int(number)
    except (TypeError, ValueError, OverflowError):
        return None


def _verdict(
    ok: bool,
    reason_code: str,
    reason: str,
    *,
    quality: str = "FRESH",
    checks: Optional[dict] = None,
) -> dict:
    return {
        "ok": bool(ok),
        "quality": quality,
        "reason_code": reason_code,
        "reason": reason,
        "checks": checks or {},
    }


def prepare_closed_candles(
    frame,
    timeframe: str,
    *,
    now_ms: Optional[int] = None,
    max_lag_periods: float = 1.25,
):
    """Retorna ``(frame_somente_fechado, veredito)``.

    A API normalmente inclui a vela corrente. Ela é descartada, não tratada
    como erro. Erro/UNKNOWN ocorre quando identidade, ordenação, OHLCV ou idade
    não permitem provar que o último candle fechado é utilizável.
    """
    period = timeframe_ms(timeframe)
    now = _timestamp_ms(now_ms if now_ms is not None else _now_ms())
    lag_mult = _number(max_lag_periods)
    if period is None or now is None or lag_mult is None or lag_mult < 0:
        return None, _verdict(
            False,
            "EXEC_CANDLE_CONTRACT_INVALID",
            "timeframe/relógio/limite de candle inválido",
            quality="UNKNOWN",
        )
    if frame is None or not hasattr(frame, "columns") or getattr(frame, "empty", True):
        return None, _verdict(
            False, "EXEC_CANDLE_UNAVAILABLE", "candles ausentes", quality="UNKNOWN"
        )

    required = ("timestamp", "open", "high", "low", "close", "volume")
    if any(column not in frame.columns for column in required):
        return None, _verdict(
            False,
            "EXEC_CANDLE_INVALID",
            "schema OHLCV incompleto",
            quality="UNKNOWN",
        )

    timestamps: list[int] = []
    try:
        records = frame.loc[:, list(required)].to_dict("records")
    except Exception:
        return None, _verdict(
            False, "EXEC_CANDLE_INVALID", "frame OHLCV ilegível", quality="UNKNOWN"
        )

    for row in records:
        ts = _timestamp_ms(row.get("timestamp"))
        o = _number(row.get("open"))
        h = _number(row.get("high"))
        low = _number(row.get("low"))
        close = _number(row.get("close"))
        volume = _number(row.get("volume"))
        if (
            ts is None
            or o is None
            or h is None
            or low is None
            or close is None
            or volume is None
            or min(o, h, low, close) <= 0
            or volume < 0
            or h < max(o, close, low)
            or low > min(o, close, h)
        ):
            return None, _verdict(
                False,
                "EXEC_CANDLE_INVALID",
                "OHLCV contém valor inválido/não finito",
                quality="UNKNOWN",
            )
        timestamps.append(ts)

    if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        return None, _verdict(
            False,
            "EXEC_CANDLE_SEQUENCE_INVALID",
            "timestamps duplicados ou fora de ordem",
            quality="UNKNOWN",
        )
    if timeframe != "1M" and any(
        right - left != period for left, right in zip(timestamps, timestamps[1:])
    ):
        return None, _verdict(
            False,
            "EXEC_CANDLE_GAP",
            "sequência OHLCV possui candle ausente/intervalo irregular",
            quality="UNKNOWN",
        )
    if timestamps[-1] > now + period:
        return None, _verdict(
            False,
            "EXEC_CANDLE_FUTURE",
            "timestamp de candle está no futuro",
            quality="UNKNOWN",
        )

    closed_count = sum(1 for ts in timestamps if ts + period <= now)
    if closed_count <= 0:
        return None, _verdict(
            False,
            "EXEC_CANDLE_NOT_CLOSED",
            "nenhum candle fechado disponível",
            quality="UNKNOWN",
        )
    closed = frame.iloc[:closed_count].copy()
    open_time = timestamps[closed_count - 1]
    close_time = open_time + period
    age = now - close_time
    max_age = period * lag_mult
    checks = {
        "timeframe": timeframe,
        "period_ms": period,
        "open_time_ms": open_time,
        "close_time_ms": close_time,
        "age_ms": age,
        "max_age_ms": max_age,
        "dropped_open_candles": len(frame) - closed_count,
    }
    if age < 0:
        return None, _verdict(
            False,
            "EXEC_CANDLE_NOT_CLOSED",
            "último candle ainda não fechou",
            quality="UNKNOWN",
            checks=checks,
        )
    if age > max_age:
        return None, _verdict(
            False,
            "EXEC_CANDLE_STALE",
            f"último candle fechado está vencido ({age:.0f}ms)",
            quality="STALE",
            checks=checks,
        )
    return closed, _verdict(
        True,
        "EXEC_CANDLE_FRESH",
        "último candle fechado é válido",
        checks=checks,
    )


def _context_age(
    metadata: Any,
    *,
    now_ms: int,
    max_age_ms: float,
    prefix: str,
    allow_degraded: bool = True,
) -> tuple[Optional[dict], Optional[dict]]:
    """Retorna ``(check, erro)`` para ticker/derivativos/regime."""
    if not isinstance(metadata, dict):
        return None, _verdict(
            False,
            f"EXEC_{prefix}_METADATA_MISSING",
            f"metadados de {prefix.lower()} ausentes",
            quality="UNKNOWN",
        )
    quality = str(metadata.get("quality") or "UNKNOWN").upper()
    accepted = {"FRESH"}
    if allow_degraded:
        accepted.add("DEGRADED")
    if quality not in accepted:
        return None, _verdict(
            False,
            f"EXEC_{prefix}_UNKNOWN",
            f"qualidade de {prefix.lower()} não comprovada ({quality})",
            quality="UNKNOWN",
        )
    observed = _timestamp_ms(metadata.get("observed_at_ms"))
    if observed is None:
        return None, _verdict(
            False,
            f"EXEC_{prefix}_METADATA_MISSING",
            f"timestamp de {prefix.lower()} ausente",
            quality="UNKNOWN",
        )
    age = now_ms - observed
    check = {"quality": quality, "observed_at_ms": observed, "age_ms": age}
    if age < 0:
        return check, _verdict(
            False,
            f"EXEC_{prefix}_FUTURE",
            f"timestamp de {prefix.lower()} está no futuro",
            quality="UNKNOWN",
            checks={prefix.lower(): check},
        )
    if age > max_age_ms:
        return check, _verdict(
            False,
            f"EXEC_{prefix}_STALE",
            f"{prefix.lower()} vencido ({age}ms)",
            quality="STALE",
            checks={prefix.lower(): check},
        )
    return check, None


def evaluate_entry_data_freshness(
    recommendation: dict,
    regime: dict,
    *,
    now_ms: Optional[int] = None,
    max_candle_lag_periods: float = 1.25,
    max_ticker_age_ms: float = 300_000,
    max_derivatives_age_ms: float = 300_000,
    max_regime_age_ms: float = 900_000,
) -> dict:
    """Valida somente contextos que influenciaram a recomendação.

    Candle e regime habilitado são essenciais. Ticker/derivativos são exigidos
    apenas quando aparecem na recomendação; ausência significa que não foram
    usados. Essa distinção evita bloquear o caminho server-side que não pontua
    funding/OI, sem permitir que um valor velho continue contribuindo.
    """
    now = _timestamp_ms(now_ms if now_ms is not None else _now_ms())
    if now is None or not isinstance(recommendation, dict):
        return _verdict(
            False, "EXEC_DATA_CONTRACT_INVALID", "contrato/relógio inválido", quality="UNKNOWN"
        )
    signal = recommendation.get("signal")
    if not isinstance(signal, dict):
        return _verdict(
            False, "EXEC_SIGNAL_METADATA_MISSING", "signal ausente", quality="UNKNOWN"
        )
    rec_symbol = recommendation.get("symbol")
    signal_symbol = signal.get("symbol")
    rec_timeframe = recommendation.get("timeframe")
    signal_timeframe = signal.get("timeframe")
    if (
        not isinstance(rec_symbol, str)
        or not rec_symbol
        or signal_symbol != rec_symbol
        or not isinstance(rec_timeframe, str)
        or signal_timeframe != rec_timeframe
    ):
        return _verdict(
            False,
            "EXEC_SIGNAL_IDENTITY_MISMATCH",
            "símbolo/timeframe da recomendação diverge do signal",
            quality="UNKNOWN",
        )
    timeframe = rec_timeframe
    period = timeframe_ms(timeframe)
    signal_open = _timestamp_ms(signal.get("timestamp"))
    if period is None or signal_open is None:
        return _verdict(
            False,
            "EXEC_CANDLE_METADATA_MISSING",
            "timeframe/timestamp do sinal ausente",
            quality="UNKNOWN",
        )

    freshness = signal.get("data_freshness")
    if not isinstance(freshness, dict):
        return _verdict(
            False,
            "EXEC_CANDLE_METADATA_MISSING",
            "prova de freshness do sinal ausente",
            quality="UNKNOWN",
        )
    candle = freshness.get("candle")
    if not isinstance(candle, dict):
        return _verdict(
            False,
            "EXEC_CANDLE_METADATA_MISSING",
            "prova do candle ausente",
            quality="UNKNOWN",
        )
    meta_open = _timestamp_ms(candle.get("open_time_ms"))
    meta_close = _timestamp_ms(candle.get("close_time_ms"))
    expected_close = signal_open + period
    if (
        candle.get("symbol") != rec_symbol
        or candle.get("timeframe") != timeframe
        or meta_open != signal_open
        or meta_close != expected_close
    ):
        return _verdict(
            False,
            "EXEC_CANDLE_IDENTITY_MISMATCH",
            "candle do sinal diverge da prova de freshness",
            quality="UNKNOWN",
        )
    if str(candle.get("quality") or "UNKNOWN").upper() != "FRESH":
        return _verdict(
            False, "EXEC_CANDLE_UNKNOWN", "qualidade do candle não comprovada", quality="UNKNOWN"
        )
    limits = {
        "candle": _number(max_candle_lag_periods),
        "ticker": _number(max_ticker_age_ms),
        "derivatives": _number(max_derivatives_age_ms),
        "regime": _number(max_regime_age_ms),
    }
    if any(value is None or value < 0 for value in limits.values()):
        return _verdict(
            False,
            "EXEC_DATA_CONTRACT_INVALID",
            "limite de freshness inválido",
            quality="UNKNOWN",
        )

    candle_age = now - expected_close
    candle_check = {
        "open_time_ms": signal_open,
        "close_time_ms": expected_close,
        "age_ms": candle_age,
        "max_age_ms": period * limits["candle"],
    }
    if candle_age < 0:
        return _verdict(
            False,
            "EXEC_CANDLE_NOT_CLOSED",
            "candle do sinal ainda está aberto",
            quality="UNKNOWN",
            checks={"candle": candle_check},
        )
    if candle_age > period * limits["candle"]:
        return _verdict(
            False,
            "EXEC_CANDLE_STALE",
            "candle do sinal venceu antes da entrada",
            quality="STALE",
            checks={"candle": candle_check},
        )

    checks: dict[str, Any] = {"candle": candle_check}

    ticker_used = (
        recommendation.get("quote_vol_usd") is not None
        or recommendation.get("spread_pct") is not None
    )
    if ticker_used:
        ticker_meta = freshness.get("ticker")
        if not isinstance(ticker_meta, dict) or ticker_meta.get("symbol") != rec_symbol:
            return _verdict(
                False,
                "EXEC_TICKER_IDENTITY_MISMATCH",
                "ticker pertence a outro símbolo ou não possui identidade",
                quality="UNKNOWN",
                checks=checks,
            )
        ticker_check, error = _context_age(
            ticker_meta,
            now_ms=now,
            max_age_ms=limits["ticker"],
            prefix="TICKER",
        )
        if error:
            error.setdefault("checks", {}).update(checks)
            return error
        checks["ticker"] = ticker_check

    derivatives = signal.get("derivatives")
    # O objeto pode existir apenas com defaults neutros quando os três feeds
    # falharam. Nesse caso ele não influenciou score/direção e não deve virar
    # uma dependência artificial. Qualquer valor real de funding/OI o torna
    # contexto usado e, então, sua prova temporal passa a ser obrigatória.
    derivatives_used = isinstance(derivatives, dict) and any(
        derivatives.get(key) is not None
        for key in ("funding_rate", "funding_rate_pct", "open_interest", "oi_change_24h_pct")
    )
    if derivatives_used:
        derivatives_meta = freshness.get("derivatives")
        if (
            not isinstance(derivatives_meta, dict)
            or derivatives_meta.get("symbol") != rec_symbol
            or derivatives.get("symbol") != rec_symbol
        ):
            return _verdict(
                False,
                "EXEC_DERIVATIVES_IDENTITY_MISMATCH",
                "derivativos pertencem a outro símbolo ou não possuem identidade",
                quality="UNKNOWN",
                checks=checks,
            )
        derivatives_check, error = _context_age(
            derivatives_meta,
            now_ms=now,
            max_age_ms=limits["derivatives"],
            prefix="DERIVATIVES",
        )
        if error:
            error.setdefault("checks", {}).update(checks)
            return error
        checks["derivatives"] = derivatives_check

    mtf = signal.get("mtf")
    higher_tfs = mtf.get("higher_tfs") if isinstance(mtf, dict) else None
    if higher_tfs:
        if not isinstance(higher_tfs, list):
            return _verdict(
                False, "EXEC_MTF_METADATA_MISSING", "contrato MTF inválido", quality="UNKNOWN",
                checks=checks,
            )
        mtf_checks = []
        for higher in higher_tfs:
            higher_tf = higher.get("timeframe") if isinstance(higher, dict) else None
            higher_period = timeframe_ms(higher_tf)
            proof = higher.get("data_freshness") if isinstance(higher, dict) else None
            higher_candle = proof.get("candle") if isinstance(proof, dict) else None
            if not isinstance(higher_candle, dict) or higher_period is None:
                return _verdict(
                    False,
                    "EXEC_MTF_METADATA_MISSING",
                    "prova temporal de timeframe superior ausente",
                    quality="UNKNOWN",
                    checks=checks,
                )
            higher_open = _timestamp_ms(higher_candle.get("open_time_ms"))
            higher_close = _timestamp_ms(higher_candle.get("close_time_ms"))
            if (
                higher_candle.get("quality") != "FRESH"
                or higher_candle.get("symbol") != rec_symbol
                or higher_candle.get("timeframe") != higher_tf
                or higher_open is None
                or higher_close != higher_open + higher_period
            ):
                return _verdict(
                    False,
                    "EXEC_MTF_IDENTITY_MISMATCH",
                    "timeframe superior diverge da prova de freshness",
                    quality="UNKNOWN",
                    checks=checks,
                )
            higher_age = now - higher_close
            higher_check = {
                "timeframe": higher_tf,
                "close_time_ms": higher_close,
                "age_ms": higher_age,
                "max_age_ms": higher_period * limits["candle"],
            }
            if higher_age < 0:
                return _verdict(
                    False, "EXEC_MTF_NOT_CLOSED", "candle MTF ainda aberto",
                    quality="UNKNOWN", checks={**checks, "mtf": [higher_check]},
                )
            if higher_age > higher_period * limits["candle"]:
                return _verdict(
                    False, "EXEC_MTF_STALE", "candle MTF venceu antes da entrada",
                    quality="STALE", checks={**checks, "mtf": [higher_check]},
                )
            mtf_checks.append(higher_check)
        checks["mtf"] = mtf_checks

    regime_enabled = not (
        isinstance(regime, dict) and regime.get("filter_enabled") is False
    )
    if regime_enabled:
        regime_check, error = _context_age(
            regime,
            now_ms=now,
            max_age_ms=limits["regime"],
            prefix="REGIME",
        )
        if error:
            error.setdefault("checks", {}).update(checks)
            return error
        checks["regime"] = regime_check
    else:
        checks["regime"] = {"quality": "DISABLED"}

    # Macro amplo (DXY/SPX/Nasdaq) não participa do caminho automático. O regime
    # acima cobre o contexto macro efetivamente usado (BTC/dominância). Logo,
    # não há valor macro silenciosamente velho contribuindo para esta entrada.
    checks["execution_price"] = {"guard": "P04A/P04B"}
    return _verdict(
        True,
        "EXEC_DATA_FRESHNESS_OK",
        "dados essenciais e contextos usados estão válidos",
        checks=checks,
    )
