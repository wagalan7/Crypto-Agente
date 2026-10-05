"""R13 — comparação de SELEÇÃO: dois caminhos de verdade sobre a MESMA população.

Para `SELECTION_ONLY`, baseline e candidata recebem a MESMA população bruta, os
MESMOS dados conhecidos, os MESMOS custos e a MESMA gestão congelada. O que
muda é só a decisão de selecionar:

  • **baseline** usa a decisão que o champion REALMENTE tomou no instante
    observado (`OBSERVED_CHAMPION_DECISION`). Reconstruir a baseline de hoje
    não representa a configuração histórica — então nada é reconstruído;
  • **candidata** roda o motor dela de verdade (`SCORE_V3_MIN_SCORE` ⇒
    `score_v3_service.score` com o corte CONGELADO no manifesto).

Regras duras:
  • feature ausente/insuficiente é `UNKNOWN`, nunca `REJECTED` nem `SELECTED`;
  • `UNKNOWN` de QUALQUER lado exclui a linha dos DOIS lados (simetria) e entra
    na cobertura — não se troca `baseline=False` por `True`;
  • nada aqui lê outcome, trajetória ou resultado: a seleção é decidida com o
    que existia na decisão. Replay e métrica vêm depois, em motores separados;
  • escopo/regra não implementada é RECUSA explícita, nunca fallback.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

SELECTION_VERSION = "R13_SELECTION_COMPARISON_V1"

STATE_SELECTED = "SELECTED"
STATE_REJECTED = "REJECTED"
STATE_UNKNOWN = "UNKNOWN"

OK = "OK"
ROW_INVALID = "POPULATION_ROW_INVALID"
FEATURES_MISSING = "POINT_IN_TIME_FEATURES_MISSING"
OUTCOME_MISSING = "OBSERVED_DECISION_MISSING"
SCORE_UNAVAILABLE = "SCORE_UNAVAILABLE"
RULE_NOT_IMPLEMENTED = "SELECTION_RULE_NOT_IMPLEMENTED"
SCOPE_NOT_IMPLEMENTED = "COMPARISON_SCOPE_NOT_IMPLEMENTED"
MANIFEST_NOT_AUTHORIZED = "MANIFEST_NOT_AUTHORIZED"

#: Campos mínimos de uma linha de população (exportada pelo R10B).
ROW_REQUIRED = ("opportunity_key", "symbol", "side", "decision_ts_ms")
#: Níveis exigidos para a linha poder entrar num replay depois.
ROW_LEVELS = ("entry", "stop_loss", "tp1", "tp2")


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    valor = float(value)
    return valor if valor == valor and abs(valor) != float("inf") else None


def _row_identity(row: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not isinstance(row, Mapping):
        return None, ROW_INVALID
    faltando = [campo for campo in ROW_REQUIRED if row.get(campo) in (None, "")]
    if faltando:
        return None, ROW_INVALID
    lado = str(row.get("side") or "").strip().lower()
    if lado not in ("long", "short"):
        return None, ROW_INVALID
    instante = row.get("decision_ts_ms")
    if isinstance(instante, bool) or not isinstance(instante, (int, float)):
        return None, ROW_INVALID
    return {"opportunity_key": str(row["opportunity_key"]),
            "symbol": str(row["symbol"]), "side": lado,
            "decision_ts_ms": int(instante)}, None


def _baseline_decision(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Decisão OBSERVADA do champion — aceita, vetada ou desconhecida."""
    observado = row.get("observed_outcome")
    rotulo = str(observado or "").strip().upper()
    if rotulo == "ACCEPTED":
        return {"state": STATE_SELECTED, "reason_code": OK,
                "provenance": "OBSERVED_CHAMPION_DECISION"}
    if rotulo == "VETOED":
        return {"state": STATE_REJECTED,
                "reason_code": str((row.get("funnel") or {}).get("first_blocker_reason")
                                   or "OBSERVED_VETO"),
                "provenance": "OBSERVED_CHAMPION_DECISION"}
    return {"state": STATE_UNKNOWN, "reason_code": OUTCOME_MISSING,
            "provenance": "OBSERVED_CHAMPION_DECISION"}


def _candidate_decision(row: Mapping[str, Any], *, rule: Mapping[str, Any]
                        ) -> Dict[str, Any]:
    """Decisão REAL da candidata pelo motor declarado no manifesto."""
    from services import score_v3_service as s3
    features = row.get("features")
    if not isinstance(features, Mapping) or not features:
        return {"state": STATE_UNKNOWN, "reason_code": FEATURES_MISSING,
                "provenance": rule.get("kind")}
    limpo = {chave: valor for chave, valor in features.items()
             if _number(valor) is not None}
    payload = s3.score(limpo, playbook=str(rule.get("playbook")),
                       side=str(row.get("side")))
    if payload.get("state") != s3.STATE_OK or payload.get("score") is None:
        return {"state": STATE_UNKNOWN, "reason_code": SCORE_UNAVAILABLE,
                "detail": list(payload.get("reason_codes") or ()),
                "provenance": rule.get("kind"),
                "model_fingerprint": payload.get("model_fingerprint")}
    corte = _number(rule.get("min_score"))
    score = _number(payload.get("score"))
    if corte is None or score is None:
        return {"state": STATE_UNKNOWN, "reason_code": SCORE_UNAVAILABLE,
                "provenance": rule.get("kind")}
    selecionado = score >= corte
    return {"state": STATE_SELECTED if selecionado else STATE_REJECTED,
            "reason_code": OK if selecionado else "BELOW_MIN_SCORE",
            "score": score, "min_score": corte,
            "provenance": rule.get("kind"),
            "model_fingerprint": payload.get("model_fingerprint")}


def decide_row(row: Mapping[str, Any], *, rule: Mapping[str, Any],
               side_label: str) -> Dict[str, Any]:
    """Decisão de UM lado para UMA linha, pela regra congelada do manifesto."""
    from services import research_manifest_service as rm
    kind = str((rule or {}).get("kind") or "")
    if kind == rm.RULE_OBSERVED_CHAMPION:
        return {**_baseline_decision(row), "side_label": side_label}
    if kind == rm.RULE_SCORE_V3_MIN:
        return {**_candidate_decision(row, rule=rule), "side_label": side_label}
    return {"state": STATE_UNKNOWN, "reason_code": RULE_NOT_IMPLEMENTED,
            "side_label": side_label, "provenance": kind or None}


def compare_population(rows: Sequence[Mapping[str, Any]], *,
                       manifest: Mapping[str, Any]) -> Dict[str, Any]:
    """Decide a população pelos DOIS lados e devolve conjuntos + cobertura.

    Só manifesto autorizado (TEST_ONLY ou aprovado) decide: rascunho/ausente
    devolve recusa. Escopo diferente de `SELECTION_ONLY` não é tratado aqui —
    gestão tem o caminho dela, e um candidato de seleção não pode ser validado
    como candidato de gestão.
    """
    from services import research_manifest_service as rm
    verdict = rm.authorized_comparison(manifest)
    if not verdict.get("available"):
        return {"ok": False, "reason_code": verdict.get("reason_code"),
                "state": verdict.get("state"),
                "decision_required": verdict.get("decision_required")}
    if verdict.get("comparison_scope") != rm.SCOPE_SELECTION:
        return {"ok": False, "reason_code": SCOPE_NOT_IMPLEMENTED,
                "detail": verdict.get("comparison_scope")}
    manifesto = rm.verify_manifest(manifest)["manifest"]
    regras = {"baseline": manifesto["baseline"]["selection_rule"],
              "candidate": manifesto["candidate"]["selection_rule"]}
    lados: Dict[str, List[Dict[str, Any]]] = {"baseline": [], "candidate": []}
    contagem = {lado: {STATE_SELECTED: 0, STATE_REJECTED: 0, STATE_UNKNOWN: 0}
                for lado in regras}
    decisoes: List[Dict[str, Any]] = []
    cobertura = {"rows_total": 0, "rows_invalid": 0, "excluded_symmetric": 0,
                 "excluded_reasons": {}, "rows_decided": 0,
                 "levels_missing": 0}
    for bruta in rows or ():
        cobertura["rows_total"] += 1
        identidade, motivo = _row_identity(bruta)
        if identidade is None:
            cobertura["rows_invalid"] += 1
            cobertura["excluded_reasons"][motivo] = \
                cobertura["excluded_reasons"].get(motivo, 0) + 1
            continue
        por_lado = {lado: decide_row(bruta, rule=regras[lado], side_label=lado)
                    for lado in regras}
        for lado, decisao in por_lado.items():
            contagem[lado][decisao["state"]] += 1
        registro = {**identidade,
                    "baseline": por_lado["baseline"], "candidate": por_lado["candidate"]}
        # Simetria: UNKNOWN de um lado tira a linha dos DOIS. Não se converte
        # desconhecido em rejeição (nem em seleção) para "salvar" cobertura.
        desconhecidos = [lado for lado, item in por_lado.items()
                         if item["state"] == STATE_UNKNOWN]
        if desconhecidos:
            cobertura["excluded_symmetric"] += 1
            for lado in desconhecidos:
                chave = f"{lado}:{por_lado[lado]['reason_code']}"
                cobertura["excluded_reasons"][chave] = \
                    cobertura["excluded_reasons"].get(chave, 0) + 1
            registro["included"] = False
            decisoes.append(registro)
            continue
        niveis = {campo: _number(bruta.get(campo)) for campo in ROW_LEVELS}
        if any(valor is None for valor in niveis.values()):
            # Sem níveis não existe replay possível: a linha é inviável para
            # AMBOS os lados, e isso é cobertura — não vantagem de um lado.
            cobertura["levels_missing"] += 1
            cobertura["excluded_symmetric"] += 1
            registro["included"] = False
            registro["excluded_reason"] = "LEVELS_MISSING"
            decisoes.append(registro)
            continue
        registro["included"] = True
        cobertura["rows_decided"] += 1
        decisoes.append(registro)
        for lado, item in por_lado.items():
            if item["state"] == STATE_SELECTED:
                lados[lado].append({**identidade, **niveis,
                                    "atr": _number(bruta.get("atr")),
                                    "selected_by": item.get("provenance")})
    base_total = cobertura["rows_decided"] or 0
    return {
        "ok": True, "version": SELECTION_VERSION,
        "scope": rm.SCOPE_SELECTION,
        "study_id": verdict.get("study_id"),
        "manifest_hash": verdict.get("manifest_hash"),
        "real_study_allowed": verdict.get("real_study_allowed"),
        "rules": {lado: dict(regra) for lado, regra in regras.items()},
        "selected": {lado: list(linhas) for lado, linhas in lados.items()},
        "counts": contagem,
        "coverage": {**cobertura,
                     "coverage_pct": (round(100.0 * base_total / cobertura["rows_total"], 4)
                                      if cobertura["rows_total"] else None)},
        "decisions": decisoes,
        # Mesma população bruta, mesmos dados conhecidos, mesma gestão.
        "same_population_both_sides": True,
        "outcomes_consulted": False,
    }


def replay_candidates(selected: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Converte o conjunto selecionado de um lado em entrada do replay R10A."""
    saida = []
    for linha in selected or ():
        saida.append({"opportunity_id": linha["opportunity_key"],
                      "symbol": linha["symbol"], "direction": linha["side"],
                      "decision_ts_ms": linha["decision_ts_ms"],
                      "entry": linha["entry"], "stop_loss": linha["stop_loss"],
                      "tp1": linha["tp1"], "tp2": linha["tp2"],
                      "atr": linha.get("atr")})
    return saida


def selection_manifest(result: Mapping[str, Any]) -> Dict[str, Any]:
    """Manifesto do contraste de SELEÇÃO — identidade e cobertura, sem métrica."""
    if not isinstance(result, Mapping) or not result.get("ok"):
        return {"state": "BLOCKED", "reason_code": (result or {}).get("reason_code")}
    return {
        "version": SELECTION_VERSION, "scope": result["scope"],
        "study_id": result.get("study_id"),
        "manifest_hash": result.get("manifest_hash"),
        "rules": result["rules"],
        "selected_counts": {lado: len(linhas)
                            for lado, linhas in result["selected"].items()},
        "coverage": result["coverage"],
        "proves": ["efeito da SELEÇÃO sobre a mesma população bruta"],
        "does_not_prove": ["gestão de saídas", "custos observados da conta",
                           "aprovação econômica"],
        "outcomes_consulted": False,
    }
