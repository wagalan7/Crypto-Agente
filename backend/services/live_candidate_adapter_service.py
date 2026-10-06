"""Lote 03: adaptador explícito de seleção, nunca de gestão ou sizing.

LEGACY não consulta autoridade, calibração V3 nem constrói contexto novo.
CANDIDATE usa os sinais ALL-TF que o scanner já calculou, escolhe o maior
score V3 aprovado e conserva geometria, tier e TODOS os gates operacionais.
Portanto o replay da população bruta NÃO prova equivalência dos vetos runtime:
essa fidelidade deve ser medida prospectivamente antes do canário real.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import time
from collections.abc import Mapping

VERSION = "R13_OPERATIONAL_SELECTION_V1"
SELECTOR_ENV = "R13_OPERATIONAL_SELECTOR"
EXPERIMENT_ENV = "R13_OPERATIONAL_EXPERIMENT_ID"
SEMANTICS = {"population": "R09_PRE_SELECTION_POPULATION", "scope": "SELECTION_ONLY",
             "tf_selection": "MAX_CANDIDATE_SCORE", "safety": "LEGACY_UNCHANGED",
             "management": "LEGACY_UNCHANGED"}

#: Significado ÚNICO de cada valor do seletor (documentado e testado):
#:   LEGACY  (default) — champion intocado; NADA de autoridade/contexto novo;
#:   OFF               — mesma PARIDADE do LEGACY, escrito explicitamente por
#:                       quem já preparou a candidata e a mantém desligada;
#:   CANDIDATE         — exige autoridade válida; inválida BLOQUEIA entradas
#:                       novas e nunca volta permissivamente ao champion;
#:   qualquer outro    — INVALID, que também bloqueia.
PARITY_MODES = ("LEGACY", "OFF")
#: Quotes e timeframes oficiais aceitos na identidade congelada.
QUOTES = ("USDT", "USDC")
#: Campos ESTÁVEIS do despacho. `observed_at_ms`, `local_fence` e
#: `authority_hash` são provas de VALIDADE de uma leitura — não identidade.
IDENTITY_FIELDS = ("experiment_id", "bundle_hash", "approval_id", "generation",
                   "model_fingerprint", "score", "min_score", "probability",
                   "probability_event", "symbol", "side", "timeframe",
                   "entry", "stop_loss", "tp1", "tp2")


def selected_mode():
    """Interpretação ÚNICA: a mesma função da governança decide o modo."""
    from services import operational_governance_service as governance
    return governance.selected_mode()


def candidate_mode_active(view):
    """Só uma visão OK e em modo CANDIDATE habilita a seleção candidata."""
    return isinstance(view, Mapping) and view.get("ok") is True \
        and view.get("mode") == "CANDIDATE"


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _hash(value):
    try:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError):
        return None


async def load_operational_view(factory, *, purpose="CANARY", now_ms=None):
    """Sem fallback: configuração CANDIDATE inválida não volta ao champion.

    LEGACY e OFF são paridade: não abrem sessão, não consultam governança e não
    constroem contexto. O identificador do experimento é lido e validado UMA
    vez, dentro da governança (ENV textual nunca chega a comparação de inteiro).
    """
    mode = selected_mode()
    if mode in PARITY_MODES:
        return {"ok": True, "mode": mode}
    if mode != "CANDIDATE":
        return {"ok": False, "reason_code": "OPERATIONAL_SELECTOR_INVALID"}
    try:
        from services import operational_governance_service as governance
        return await governance.load_view(factory, purpose=purpose,
            now_ms=int(time.time() * 1000) if now_ms is None else now_ms)
    except Exception:
        return {"ok": False, "reason_code": "CANDIDATE_AUTHORITY_UNAVAILABLE"}


def candidate_decision(authority, *, features, symbol, side, timeframe,
                       entry, stop_loss, tp1, tp2, now_ms=None):
    """Só o motor real V3 e a faixa do evento EXATO do artefato OOS."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    try:
        from services import score_v3_service as score_v3
        from services import score_v3_calibration_service as calibration
        if not isinstance(authority, Mapping) or authority.get("ok") is not True:
            raise ValueError("CANDIDATE_AUTHORITY_UNAVAILABLE")
        bundle = authority["bundle"]
        if bundle.get("operational_semantics") != SEMANTICS:
            raise ValueError("OPERATIONAL_SEMANTICS_MISMATCH")
        manifest = bundle["manifest"]
        candidate = manifest["candidate"]
        rule = candidate["selection_rule"]
        if manifest["comparison_scope"] != "SELECTION_ONLY" or rule["kind"] != "SCORE_V3_MIN_SCORE":
            raise ValueError("CANDIDATE_SCOPE_NOT_IMPLEMENTED")
        limits = authority["approval"]["limits"]
        if symbol not in limits["symbols"] or rule["playbook"] not in limits["playbooks"]:
            raise ValueError("CANARY_POPULATION_OUTSIDE_APPROVAL")
        if now >= limits["expires_at_ms"]:
            raise ValueError("CANARY_EXPIRED")
        payload = score_v3.score(features, playbook=rule["playbook"], side=side,
            config=score_v3.ScoreConfig(**candidate["score_config"]))
        value, minimum = _number(payload.get("score")), _number(rule["min_score"])
        if payload.get("state") != score_v3.STATE_OK or value is None or minimum is None:
            raise ValueError("CANDIDATE_SCORE_UNAVAILABLE")
        if value < minimum:
            return {"ok": False, "reason_code": "CANDIDATE_BELOW_MIN_SCORE", "score": value}
        artifact = bundle["calibration_artifact"]
        probability = calibration.predict(artifact, score=value,
            model_fingerprint=payload["model_fingerprint"],
            score_config_hash=candidate["score_config_hash"],
            population=score_v3.ScoreConfig(**candidate["score_config"]).population,
            event=artifact.get("event"), now_ms=now)
        if probability.get("available") is not True or probability.get("out_of_sample") is not True:
            raise ValueError("CANDIDATE_CALIBRATION_UNAVAILABLE")
        event = probability["event"]
        selection = {"score": value, "min_score": minimum,
            "probability_event": event, "probability": probability["probability"],
            "prob_tp1": probability["probability"] if event == calibration.EVENT_TP1 else None,
            "prob_tp2": probability["probability"] if event == calibration.EVENT_TP2 else None,
            "model_fingerprint": payload["model_fingerprint"],
            "features": copy.deepcopy(dict(features)), "feature_producer": "R13_POINT_IN_TIME_FEATURES_V1",
            "identity": {"symbol": symbol, "side": side, "timeframe": timeframe,
                "entry": entry, "stop_loss": stop_loss, "tp1": tp1, "tp2": tp2},
            "runtime_semantics": dict(SEMANTICS), "tier_source": "LEGACY_UNCHANGED"}
        context = {"version": VERSION, "authority": copy.deepcopy(dict(authority)),
                   "selection": selection}
        context["selection_hash"] = _hash(context)
        if context["selection_hash"] is None:
            raise ValueError("CANDIDATE_CONTEXT_INVALID")
        return {"ok": True, "score": value, "context": context}
    except (KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "reason_code": str(exc) if isinstance(exc, ValueError)
                else "CANDIDATE_CONTEXT_INVALID"}


def _price(value):
    """Preço utilizável: número finito e POSITIVO. Texto/bool nunca passam."""
    number = _number(value)
    return number if number is not None and number > 0 else None


def _validate_identity(identity):
    """Geometria, lado, símbolo/quote e timeframe conferidos de forma FECHADA.

    Nada aqui confia num hash produzido pelo próprio caller: os valores são
    revalidados contra o catálogo oficial de timeframes e a geometria exigida
    pelo lado. Mercado/perna viajam explícitos no símbolo e no `position_side`
    legado do executor, que esta camada não altera.
    """
    from services import preselection_observation_service as pre
    if not isinstance(identity, Mapping) or set(identity) != {
            "symbol", "side", "timeframe", "entry", "stop_loss", "tp1", "tp2"}:
        raise ValueError("CANDIDATE_IDENTITY_INVALID")
    side = identity.get("side")
    if side not in ("long", "short"):
        raise ValueError("CANDIDATE_SIDE_INVALID")
    symbol = identity.get("symbol")
    if not isinstance(symbol, str) or symbol != symbol.strip().upper() \
            or not any(symbol.endswith(quote) and len(symbol) > len(quote)
                       for quote in QUOTES):
        raise ValueError("CANDIDATE_SYMBOL_INVALID")
    if identity.get("timeframe") not in pre.TIMEFRAME_MINUTES:
        raise ValueError("CANDIDATE_TIMEFRAME_INVALID")
    levels = {name: _price(identity.get(name))
              for name in ("entry", "stop_loss", "tp1", "tp2")}
    if any(value is None for value in levels.values()):
        raise ValueError("CANDIDATE_PRICES_INVALID")
    entry, stop, tp1, tp2 = (levels["entry"], levels["stop_loss"],
                             levels["tp1"], levels["tp2"])
    ordered = (stop < entry < tp1 < tp2) if side == "long" else (stop > entry > tp1 > tp2)
    if not ordered:
        raise ValueError("CANDIDATE_GEOMETRY_INVALID")
    return {"symbol": symbol, "side": side, "timeframe": identity["timeframe"],
            **levels}


def freeze_context(context, *, require_purpose="CANARY"):
    """Integridade E conteúdo da seleção aditiva, conferidos de ponta a ponta.

    Legado nunca precisa deste contrato. Aqui a prova não é só o hash: lado,
    símbolo/quote, timeframe, preços, geometria, score e probabilidade (do
    evento EXATO do artefato) são revalidados — adulterar e re-selar não passa.

    `require_purpose` é a fronteira entre observação e operação: o caminho que
    AUTORIZA intenção/ordem exige `CANARY`; a decisão observacional do Shadow
    usa `SHADOW` e nunca é aceita como autoridade operacional.
    """
    if not isinstance(context, Mapping) or set(context) != {"version", "authority", "selection", "selection_hash"}:
        raise ValueError("CANDIDATE_CONTEXT_INVALID")
    if context.get("version") != VERSION or not isinstance(context.get("authority"), Mapping):
        raise ValueError("CANDIDATE_CONTEXT_INVALID")
    if require_purpose is not None \
            and context["authority"].get("purpose") != require_purpose:
        raise ValueError("CANDIDATE_PURPOSE_MISMATCH")
    body = {k: v for k, v in context.items() if k != "selection_hash"}
    if _hash(body) is None or _hash(body) != context.get("selection_hash"):
        raise ValueError("CANDIDATE_CONTEXT_TAMPERED")
    selection = context.get("selection")
    if not isinstance(selection, Mapping) or selection.get("runtime_semantics") != SEMANTICS:
        raise ValueError("OPERATIONAL_SEMANTICS_MISMATCH")
    score, minimum = _number(selection.get("score")), _number(selection.get("min_score"))
    if score is None or minimum is None or not 0 <= minimum <= score <= 100:
        raise ValueError("CANDIDATE_SCORE_UNAVAILABLE")
    if not isinstance(selection.get("model_fingerprint"), str) \
            or not selection["model_fingerprint"].strip():
        raise ValueError("CANDIDATE_MODEL_UNKNOWN")
    _validate_identity(selection.get("identity"))
    probability = _number(selection.get("probability"))
    if probability is None or not 0.0 <= probability <= 1.0:
        raise ValueError("CANDIDATE_PROBABILITY_INVALID")
    artifact = ((context["authority"].get("bundle") or {})
                .get("calibration_artifact") or {})
    event = selection.get("probability_event")
    if not isinstance(event, str) or event != artifact.get("event"):
        # Probabilidade de OUTRO evento não descreve esta decisão.
        raise ValueError("CANDIDATE_PROBABILITY_EVENT_MISMATCH")
    from services import score_v3_calibration_service as calibration
    for campo, alvo in (("prob_tp1", calibration.EVENT_TP1),
                        ("prob_tp2", calibration.EVENT_TP2)):
        valor = selection.get(campo)
        if event == alvo:
            if _number(valor) != probability:
                raise ValueError("CANDIDATE_PROBABILITY_EVENT_MISMATCH")
        elif valor is not None:
            raise ValueError("CANDIDATE_PROBABILITY_EVENT_MISMATCH")
    return copy.deepcopy(dict(context))


SHADOW_SCOPE = "CANDIDATE_SHADOW"
SHADOW_DECISION_VERSION = "R13_CANDIDATE_SHADOW_DECISION_V1"


async def load_shadow_authority(factory):
    """Autoridade SHADOW para OBSERVAÇÃO — independente do seletor operacional.

    Fail-soft: qualquer indisponibilidade devolve `None` e o scanner segue
    exatamente como antes (pesquisa não derruba o caminho do bot).
    """
    try:
        from services import prospective_shadow_service as prospective
        return await prospective.shadow_authority(factory)
    except Exception:  # noqa: BLE001
        return None


def shadow_group_decision(authority, candidates, *, now_ms=None):
    """Decisão OBSERVACIONAL da candidata sobre o grupo ALL-TF do símbolo.

    Usa a MESMA função de decisão do caminho operacional (`candidate_decision`)
    — nenhuma cópia "equivalente" do algoritmo —, com autoridade de propósito
    **SHADOW**: calcula sem efeito externo, sem exigir promoção/ELIGIBLE e sem
    nunca virar autoridade de ordem. A escolha entre TFs é a regra congelada
    `MAX_CANDIDATE_SCORE`, aplicada aos MESMOS TFs avaliados pelo champion.

    Devolve o grupo inteiro: avaliados, rejeitados por score CONHECIDO
    (`REJECTED`) e indeterminados por feature/modelo ausente (`UNKNOWN`) — que
    não é acerto nem discordância.
    """
    if not isinstance(authority, Mapping) or authority.get("ok") is not True \
            or authority.get("purpose") != "SHADOW":
        return {"ok": False, "reason_code": "SHADOW_AUTHORITY_UNAVAILABLE"}
    now = int(time.time() * 1000) if now_ms is None else now_ms
    manifesto = ((authority.get("bundle") or {}).get("manifest") or {})
    candidata = manifesto.get("candidate") or {}
    avaliados = []
    vencedor = None
    for item in candidates or ():
        if not isinstance(item, Mapping) or not item.get("timeframe"):
            continue
        decision = candidate_decision(
            authority, features=item.get("features"), symbol=item.get("symbol"),
            side=item.get("side"), timeframe=item.get("timeframe"),
            entry=item.get("entry"), stop_loss=item.get("stop_loss"),
            tp1=item.get("tp1"), tp2=item.get("tp2"), now_ms=now)
        linha = {"timeframe": str(item["timeframe"]),
                 "score": _number(decision.get("score")),
                 "reason_code": decision.get("reason_code")}
        if decision.get("ok") is True:
            contexto = decision["context"]
            selecao = contexto["selection"]
            linha.update(state="SELECTED", min_score=selecao.get("min_score"),
                         probability=selecao.get("probability"),
                         probability_event=selecao.get("probability_event"),
                         model_fingerprint=selecao.get("model_fingerprint"))
            if vencedor is None or (linha["score"] or 0) > (vencedor["score"] or 0):
                vencedor = {**linha, "context": contexto}
        elif decision.get("reason_code") == "CANDIDATE_BELOW_MIN_SCORE":
            # Recusa por score CONHECIDO é discordância legítima, não dúvida.
            linha.update(state="REJECTED")
        else:
            linha.update(state="UNKNOWN")
        avaliados.append(linha)
    if not avaliados:
        return {"ok": False, "reason_code": "SHADOW_GROUP_EMPTY"}
    grupo = {"version": SHADOW_DECISION_VERSION, "scope": SHADOW_SCOPE,
             "rule": SEMANTICS["tf_selection"],
             "evaluated_timeframes": [linha["timeframe"] for linha in avaliados],
             "evaluated": [{k: v for k, v in linha.items() if k != "context"}
                           for linha in avaliados],
             "selected_timeframe": (vencedor or {}).get("timeframe"),
             "selected": vencedor is not None,
             "experiment_id": authority.get("experiment_id"),
             "generation": authority.get("generation"),
             "approval_id": (authority.get("approval") or {}).get("approval_id"),
             "bundle_hash": (authority.get("bundle") or {}).get("bundle_hash"),
             # Identidade de FÓRMULA/CONFIG da decisão observada: é com ela que a
             # recomputação offline se reconcilia depois (hash igual = mesma
             # regra; hash diferente = incomparável, nunca "fidelidade 0%").
             "manifest_hash": manifesto.get("manifest_hash"),
             "score_config_hash": candidata.get("score_config_hash"),
             "min_score_rule": (candidata.get("selection_rule") or {}).get("min_score"),
             "playbook": (candidata.get("selection_rule") or {}).get("playbook"),
             "decided_at_ms": now, "purpose": "SHADOW"}
    if vencedor is not None:
        selecao = vencedor["context"]["selection"]
        grupo.update(score=vencedor["score"], min_score=selecao.get("min_score"),
                     probability=selecao.get("probability"),
                     probability_event=selecao.get("probability_event"),
                     model_fingerprint=selecao.get("model_fingerprint"),
                     identity=dict(selecao.get("identity") or {}))
    grupo["group_hash"] = _hash(grupo)
    return {"ok": True, "group": grupo,
            "contexts": {linha["timeframe"]: linha.get("context")
                         for linha in avaliados if linha.get("context")}}


def shadow_decision_for_timeframe(group, timeframe):
    """Decisão observacional DESTE timeframe, no estágio exato comparado."""
    if not isinstance(group, Mapping):
        return None
    for linha in group.get("evaluated") or ():
        if isinstance(linha, Mapping) and linha.get("timeframe") == timeframe:
            return {"scope": SHADOW_SCOPE, "version": group.get("version"),
                    "rule": group.get("rule"),
                    "timeframe": timeframe, "state": linha.get("state"),
                    "score": linha.get("score"), "min_score": linha.get("min_score"),
                    "probability": linha.get("probability"),
                    "probability_event": linha.get("probability_event"),
                    "model_fingerprint": linha.get("model_fingerprint"),
                    "reason_code": linha.get("reason_code"),
                    "is_selected_timeframe": group.get("selected_timeframe") == timeframe,
                    "selected": bool(group.get("selected")
                                     and group.get("selected_timeframe") == timeframe),
                    "group": {"evaluated_timeframes": list(group.get("evaluated_timeframes") or ()),
                              "selected_timeframe": group.get("selected_timeframe"),
                              "group_hash": group.get("group_hash")},
                    "decided_at_ms": group.get("decided_at_ms"),
                    "authority": {"experiment_id": group.get("experiment_id"),
                                  "generation": group.get("generation"),
                                  "approval_id": group.get("approval_id"),
                                  "bundle_hash": group.get("bundle_hash"),
                                  "manifest_hash": group.get("manifest_hash"),
                                  "score_config_hash": group.get("score_config_hash"),
                                  "playbook": group.get("playbook"),
                                  "purpose": "SHADOW"}}
    return None


def identity_of(context):
    """Identidade ESTÁVEL do despacho — o fato congelado, não a leitura.

    Experimento, bundle, aprovação, geração, modelo e decisão (score mínimo,
    probabilidade/evento e geometria). Carimbo de leitura, fence local e
    `authority_hash` ficam FORA: eles provam validade de uma leitura, e usá-los
    como identidade faria retry, restart e filha `-mfb` parecerem outra
    candidata.
    """
    if not isinstance(context, Mapping):
        raise ValueError("CANDIDATE_CONTEXT_INVALID")
    authority = context.get("authority")
    selection = context.get("selection")
    if not isinstance(authority, Mapping) or not isinstance(selection, Mapping):
        raise ValueError("CANDIDATE_CONTEXT_INVALID")
    approval = authority.get("approval") if isinstance(authority.get("approval"), Mapping) else {}
    bundle = authority.get("bundle") if isinstance(authority.get("bundle"), Mapping) else {}
    identity = selection.get("identity") if isinstance(selection.get("identity"), Mapping) else {}
    valores = {
        "experiment_id": authority.get("experiment_id"),
        "bundle_hash": bundle.get("bundle_hash") or approval.get("bundle_hash"),
        "approval_id": approval.get("approval_id"),
        "generation": authority.get("generation"),
        "model_fingerprint": selection.get("model_fingerprint"),
        "score": selection.get("score"), "min_score": selection.get("min_score"),
        "probability": selection.get("probability"),
        "probability_event": selection.get("probability_event"),
        **{campo: identity.get(campo) for campo in
           ("symbol", "side", "timeframe", "entry", "stop_loss", "tp1", "tp2")},
    }
    faltando = [campo for campo in IDENTITY_FIELDS if valores.get(campo) is None]
    if faltando:
        raise ValueError("CANDIDATE_IDENTITY_INCOMPLETE")
    return {campo: valores[campo] for campo in IDENTITY_FIELDS}


def identity_hash(context):
    """Hash da identidade estável (serve a logs/catálogo, não à autorização)."""
    return _hash(identity_of(context))


def same_identity(first, second):
    """Duas leituras da MESMA candidata/decisão? Compara o fato, não o carimbo."""
    try:
        return identity_of(first) == identity_of(second)
    except (ValueError, TypeError):
        return False


def context_for_rec(rec):
    context = rec.get("operational_selection") or rec.get("_r13_candidate_authority")
    context = freeze_context(context)
    identity = context["selection"]["identity"]
    signal = rec.get("signal") or {}
    for key in ("symbol", "timeframe", "entry", "stop_loss", "tp2"):
        if rec.get(key) != identity.get(key):
            raise ValueError("CANDIDATE_RECOMMENDATION_MISMATCH")
    if rec.get("direction") != identity.get("side") or signal.get("tp1") != identity.get("tp1"):
        raise ValueError("CANDIDATE_RECOMMENDATION_MISMATCH")
    return context


def sync_context_valid(context):
    try:
        frozen = freeze_context(context)
        from services import operational_governance_service as governance
        verdict = governance.sync_authority_valid(frozen["authority"])
        return {'ok': verdict is True, 'reason_code': 'CANDIDATE_AUTHORITY_VALID' if verdict is True
                else 'CANDIDATE_AUTHORITY_INVALID'}
    except Exception:
        return {"ok": False, "reason_code": "CANDIDATE_AUTHORITY_UNAVAILABLE"}


async def authorize_context(factory, context):
    """Guard também antes da alavancagem; nenhuma conexão de exchange aqui."""
    try:
        frozen = freeze_context(context)
        from services import operational_governance_service as governance
        from services.entry_intent_service import acquire_risk_lock
        async with factory() as session:
            await acquire_risk_lock(session)
            result = await governance.assert_authority_in_session(session,
                frozen["authority"], int(time.time() * 1000))
            await session.rollback()
        if result.get("ok") is not True:
            return result
        final = sync_context_valid(frozen)
        if final.get("ok") is not True:
            return final
        def sync_check(_params):
            return sync_context_valid(frozen)
        return {"ok": True, "sync_check": sync_check}
    except Exception:
        return {"ok": False, "reason_code": "CANDIDATE_AUTHORITY_UNAVAILABLE"}


def canary_risk_verdict(context, *, entry, stop, qty, equity, leverage):
    """Teto aprovado reduz admissibilidade, nunca amplia sizing/alavancagem."""
    try:
        frozen = freeze_context(context)
        cap = _number(frozen["authority"]["approval"]["limits"]["max_risk_pct"])
        values = [_number(v) for v in (entry, stop, qty, equity, leverage)]
        if cap is None or cap <= 0 or any(v is None or v <= 0 for v in values):
            raise ValueError("CANARY_RISK_UNKNOWN")
        risk = abs(values[0] - values[1]) * values[2] / values[3] * 100
        if risk > cap:
            raise ValueError("CANARY_RISK_LIMIT")
        return {"ok": True, "risk_pct": risk, "max_risk_pct": cap}
    except (KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "reason_code": str(exc) if isinstance(exc, ValueError) else "CANARY_RISK_UNKNOWN"}
