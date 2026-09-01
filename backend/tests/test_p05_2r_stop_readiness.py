"""P05.2R — Stop Evidence Readiness Monitor (somente leitura).

Suíte HERMÉTICA: rede/DNS bloqueados e CONTABILIZADOS. Sem exchange, banco
externo, Railway ou credencial real. O holdout final permanece SELADO.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]

# ── Hermeticidade ───────────────────────────────────────────────────────────
import socket as _socket

_REAL_GETADDRINFO = _socket.getaddrinfo
_REAL_CREATE_CONNECTION = _socket.create_connection
_NET_ATTEMPTS: list = []


def _blocked_net(*a, **k):
    _NET_ATTEMPTS.append(a[:1])
    raise RuntimeError(f"REDE BLOQUEADA no teste P05.2R (hermético): {a[:1]}")


def setUpModule():
    _NET_ATTEMPTS.clear()
    _socket.getaddrinfo = _blocked_net
    _socket.create_connection = _blocked_net


def tearDownModule():
    _socket.getaddrinfo = _REAL_GETADDRINFO
    _socket.create_connection = _REAL_CREATE_CONNECTION
    if _NET_ATTEMPTS:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET_ATTEMPTS} tentativa(s) de rede.")


from services import strategy_evidence_service as p05      # noqa: E402


# ── Fixtures de agregados já produzidos ────────────────────────────────────
def _candidate(status=p05.LAB_VALIDATION_SUPPORTED, *, removed=25, **kw):
    base = {
        "hash": "abc123", "axis": "regime", "value": "CHOP",
        "type": "STOP_CONTEXT_BLOCK", "status": status,
        "reason_code": "ALL_VALIDATION_CHECKS_PASSED",
        "detail": "sobreviveu a todos os checks de validação",
        "executable": False, "promotable": False, "shadow_supported": False,
        "requires_future_holdout_review": True, "holdout_status": "SEALED",
        "validation": {"operations_removed": removed, "evaluable": 100},
        "wins_removed": 0, "stops_avoided": removed,
    }
    base.update(kw)
    return base


def _lab(status=p05.LAB_NO_ELIGIBLE, *, candidates=None, rejected=None, **kw):
    base = {
        "phase": "P05.2B", "execution_mode": "ANALYTICS_ONLY", "read_only": True,
        "executable": False, "promotable": False, "shadow_supported": False,
        "holdout_status": "SEALED", "holdout_outcomes_read": False,
        "holdout_metrics_computed": False, "requires_future_holdout_review": True,
        "status": status, "reason_code": "NO_PERSISTENT_ADVERSE_PATTERN",
        "detail": "ainda não há padrão persistente elegível",
        "source_patterns": [], "candidates": candidates or [],
        "rejected": rejected or [],
    }
    base.update(kw)
    return base


def _pattern(axis="regime", value="CHOP",
             classification=p05.STOP_PERSISTENT_ADVERSE):
    return {"axis": axis, "value": value, "classification": classification,
            "validation": {"stop_rate_lift_pp": 40.0, "exposure": 25},
            "train": {"stop_rate_lift_pp": 35.0, "exposure": 30}}


def _sd(*, patterns=None, lab=None, verdict=None, span=120.0, **kw):
    pats = patterns if patterns is not None else []
    base = {
        "phase": "P05.2A", "execution_mode": "ANALYTICS_ONLY", "read_only": True,
        "holdout_status": "SEALED", "holdout_outcomes_read": False,
        "holdout_metrics_computed": False,
        "sample": {"train_count": 200, "validation_count": 100, "test_count": 100,
                   "observed_span_days": span, "young_history": False},
        "persistent_patterns": pats,
        "non_persistent_patterns": [_pattern(classification=p05.STOP_MIXED)],
        "patterns_verdict": verdict or ("PERSISTENT_PATTERNS_FOUND" if pats
                                        else "NO_PERSISTENT_STOP_PATTERN"),
        "patterns_verdict_reason": "nenhum contexto se manteve adverso",
        "offline_lab": lab if lab is not None else _lab(),
        "real": {"total_closed": 12, "stops": 5, "stop_rate_pct": 41.67,
                 "expectancy_r": -0.1, "reliability": "EARLY",
                 "linked_to_snapshot_pct": 100.0},
    }
    base.update(kw)
    return base


def _diag(*, path_status="COLLECTING", lat_status="UNAVAILABLE"):
    return {
        "mae_mfe": {"status": path_status, "observed": 12, "eligible_resolved": 20,
                    "coverage_pct": 60.0, "missing": 8,
                    "unavailable_by_reason": {"sem_p05_path": 8}},
        "telemetry": {
            "latency": {"status": lat_status, "attempts_observed": 0,
                        "coverage_pct": None, "fill_auditable": 0,
                        "linked_real_trades": 0, "missing_by_reason": {}},
            "slippage": {"coverage_pct": 83.3},
        },
    }


class _SealedRow(dict):
    """Sentinela: EXPLODE se qualquer outcome do holdout for acessado."""

    def __getitem__(self, key):
        if key in ("realized_r", "status", "outcome_class", "features"):
            raise AssertionError(f"HOLDOUT VIOLADO: {key} do teste foi lido")
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key in ("realized_r", "status", "outcome_class", "features"):
            raise AssertionError(f"HOLDOUT VIOLADO: {key} do teste foi lido")
        return super().get(key, default)


# ════════════════════════════════════════════════════════════════════════════
#  Estados
# ════════════════════════════════════════════════════════════════════════════
class Estados(unittest.TestCase):

    def test_sem_padrao_vira_collecting(self):
        out = p05.build_stop_readiness(_sd(), _diag())
        self.assertEqual(out["state"], "COLLECTING")
        self.assertEqual(out["reason_code"], "NO_PERSISTENT_ADVERSE_PATTERN")
        self.assertFalse(out["ready_for_p052c"])
        self.assertIn("stop_pattern", out["blocked_by"])

    def test_padrao_com_pouca_amostra_vira_insufficient(self):
        lab = _lab(p05.LAB_INSUFFICIENT,
                   rejected=[_candidate(p05.LAB_INSUFFICIENT, removed=4)])
        out = p05.build_stop_readiness(_sd(patterns=[_pattern()], lab=lab), _diag())
        self.assertEqual(out["state"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(out["reason_code"], "INSUFFICIENT_EVIDENCE")
        self.assertFalse(out["ready_for_p052c"])

    def test_reprovacao_substantiva_vira_rejected(self):
        lab = _lab(p05.LAB_REJECTED,
                   rejected=[_candidate(p05.LAB_REJECTED, removed=40)])
        out = p05.build_stop_readiness(_sd(patterns=[_pattern()], lab=lab), _diag())
        self.assertEqual(out["state"], "HYPOTHESIS_REJECTED")
        self.assertEqual(out["reason_code"], "VALIDATION_CHECK_FAILED")
        self.assertFalse(out["ready_for_p052c"])

    def test_reprovacao_nunca_vira_coletando(self):
        lab = _lab(p05.LAB_REJECTED,
                   rejected=[_candidate(p05.LAB_REJECTED)])
        out = p05.build_stop_readiness(_sd(patterns=[_pattern()], lab=lab), _diag())
        self.assertNotEqual(out["state"], "COLLECTING")

    def test_ausencia_de_evidencia_nunca_vira_reprovacao(self):
        lab = _lab(p05.LAB_INSUFFICIENT,
                   rejected=[_candidate(p05.LAB_INSUFFICIENT, removed=2)])
        out = p05.build_stop_readiness(_sd(patterns=[_pattern()], lab=lab), _diag())
        self.assertNotEqual(out["state"], "HYPOTHESIS_REJECTED")

    def test_candidato_validado_vira_ready(self):
        lab = _lab(p05.LAB_VALIDATION_SUPPORTED, candidates=[_candidate()])
        out = p05.build_stop_readiness(_sd(patterns=[_pattern()], lab=lab), _diag())
        self.assertEqual(out["state"], "READY_FOR_P052C")
        self.assertTrue(out["ready_for_p052c"])
        self.assertEqual(out["blocked_by"], [])
        self.assertIn("MANUAL", out["next_action"] + out["detail"])

    def test_erro_estrutural_vira_unavailable(self):
        for sd in ({}, {"error": "loader falhou"},
                   _sd(patterns=[_pattern()], verdict="UNAVAILABLE"),
                   _sd(patterns=[_pattern()], lab=_lab(p05.LAB_UNAVAILABLE))):
            out = p05.build_stop_readiness(sd, _diag())
            self.assertEqual(out["state"], "UNAVAILABLE", sd.get("patterns_verdict"))
            self.assertFalse(out["ready_for_p052c"])

    def test_estados_permitidos_apenas(self):
        cenarios = [
            (_sd(), _diag()),
            (_sd(patterns=[_pattern()], lab=_lab(p05.LAB_INSUFFICIENT)), _diag()),
            (_sd(patterns=[_pattern()], lab=_lab(p05.LAB_REJECTED)), _diag()),
            (_sd(patterns=[_pattern()],
                 lab=_lab(p05.LAB_VALIDATION_SUPPORTED, candidates=[_candidate()])),
             _diag()),
            ({}, {}),
        ]
        for sd, dg in cenarios:
            out = p05.build_stop_readiness(sd, dg)
            self.assertIn(out["state"], p05.P052R_STATES)

    def test_nenhum_estado_proibido_e_emitido(self):
        lab = _lab(p05.LAB_VALIDATION_SUPPORTED, candidates=[_candidate()])
        out = p05.build_stop_readiness(_sd(patterns=[_pattern()], lab=lab), _diag())
        blob = repr(out)
        for proibido in ("APPROVED", "ELIGIBLE", "PROMOTED", '"ACTIVE"',
                         "LIVE_READY", "AUTO_PROMOTE"):
            self.assertNotIn(proibido, blob)

    def test_padrao_de_eixo_bloqueado_nao_libera(self):
        lab = _lab(p05.LAB_NO_ELIGIBLE)
        out = p05.build_stop_readiness(
            _sd(patterns=[_pattern(axis="base", value="BTC")], lab=lab), _diag())
        self.assertEqual(out["state"], "COLLECTING")

    def test_classificacoes_fracas_nunca_contam_como_prontas(self):
        for klass in (p05.STOP_MIXED, p05.STOP_SAMPLE_LIMITED,
                      p05.STOP_LOW_COVERAGE, p05.STOP_NOT_ADVERSE):
            sd = _sd(patterns=[_pattern(classification=klass)])
            out = p05.build_stop_readiness(sd, _diag())
            self.assertEqual(out["state"], "COLLECTING", klass)
            self.assertFalse(out["tracks"]["stop_pattern"]["ready"])


# ════════════════════════════════════════════════════════════════════════════
#  Regra READY_FOR_P052C
# ════════════════════════════════════════════════════════════════════════════
class RegraReady(unittest.TestCase):

    def _ready(self, **cand):
        lab = _lab(p05.LAB_VALIDATION_SUPPORTED, candidates=[_candidate(**cand)])
        return p05.build_stop_readiness(_sd(patterns=[_pattern()], lab=lab), _diag())

    def test_candidato_executavel_bloqueia(self):
        out = self._ready(executable=True)
        self.assertNotEqual(out["state"], "READY_FOR_P052C")
        self.assertEqual(out["reason_code"], "CONTRACT_INVARIANT_BROKEN")

    def test_candidato_promovivel_bloqueia(self):
        out = self._ready(promotable=True)
        self.assertNotEqual(out["state"], "READY_FOR_P052C")

    def test_sem_revisao_futura_do_holdout_bloqueia(self):
        out = self._ready(requires_future_holdout_review=False)
        self.assertNotEqual(out["state"], "READY_FOR_P052C")

    def test_holdout_nao_selado_bloqueia(self):
        lab = _lab(p05.LAB_VALIDATION_SUPPORTED, candidates=[_candidate()])
        sd = _sd(patterns=[_pattern()], lab=lab)
        sd["holdout_status"] = "OPEN"
        out = p05.build_stop_readiness(sd, _diag())
        self.assertNotEqual(out["state"], "READY_FOR_P052C")

    def test_outcome_do_holdout_lido_bloqueia(self):
        lab = _lab(p05.LAB_VALIDATION_SUPPORTED, candidates=[_candidate()])
        sd = _sd(patterns=[_pattern()], lab=lab)
        sd["holdout_outcomes_read"] = True
        out = p05.build_stop_readiness(sd, _diag())
        self.assertNotEqual(out["state"], "READY_FOR_P052C")

    def test_ready_significa_apenas_apresentar(self):
        out = self._ready()
        self.assertTrue(out["ready_for_p052c"])
        self.assertIn("não significa aprovação", out["does_not_mean"])
        self.assertIn("não abre o holdout final", out["does_not_mean"])


# ════════════════════════════════════════════════════════════════════════════
#  Trilhas
# ════════════════════════════════════════════════════════════════════════════
class Trilhas(unittest.TestCase):

    def test_cinco_trilhas_presentes(self):
        out = p05.build_stop_readiness(_sd(), _diag())
        self.assertEqual(set(out["tracks"]), {
            "stop_pattern", "offline_lab", "forward_path", "live_execution",
            "real_sample"})

    def test_trilha_p052a(self):
        t = p05.build_stop_readiness(_sd(), _diag())["tracks"]["stop_pattern"]
        self.assertEqual(t["phase"], "P05.2A")
        self.assertEqual(t["persistent_patterns"], 0)
        self.assertEqual(t["non_persistent_patterns"], 1)
        self.assertEqual(t["patterns_verdict"], "NO_PERSISTENT_STOP_PATTERN")
        self.assertEqual(set(t["eligible_axes"]), set(p05.P052B_ALLOWED_AXES))
        self.assertTrue(t["gates_p052c"])

    def test_trilha_p052b(self):
        lab = _lab(p05.LAB_REJECTED,
                   candidates=[], rejected=[_candidate(p05.LAB_REJECTED),
                                            _candidate(p05.LAB_INSUFFICIENT)])
        t = p05.build_stop_readiness(_sd(patterns=[_pattern()], lab=lab),
                                     _diag())["tracks"]["offline_lab"]
        self.assertEqual(t["phase"], "P05.2B")
        self.assertEqual(t["hypotheses_evaluated"], 2)
        self.assertEqual(t["validation_supported"], 0)
        self.assertEqual(t["rejected"], 1)
        self.assertEqual(t["insufficient"], 1)
        self.assertFalse(t["ready_for_holdout_review"])
        self.assertTrue(t["main_reason"])

    def test_trilha_p051t(self):
        t = p05.build_stop_readiness(_sd(), _diag())["tracks"]["forward_path"]
        self.assertEqual(t["phase"], "P05.1T")
        self.assertEqual(t["status"], "COLLECTING")
        self.assertEqual(t["observed"], 12)
        self.assertEqual(t["coverage_pct"], 60.0)
        self.assertEqual(t["missing_by_reason"], {"sem_p05_path": 8})
        self.assertEqual(t["min_observed"], 30)
        self.assertEqual(t["min_coverage_pct"], 80.0)
        self.assertFalse(t["gates_p052c"])

    def test_trilha_p052l(self):
        t = p05.build_stop_readiness(
            _sd(), _diag(lat_status="COLLECTING"))["tracks"]["live_execution"]
        self.assertEqual(t["phase"], "P05.2L")
        self.assertEqual(t["status"], "COLLECTING")
        self.assertEqual(t["attempts_observed"], 0)
        self.assertEqual(t["min_observed"], 30)
        self.assertFalse(t["gates_p052c"])

    def test_trilha_real_separada_do_shadow(self):
        t = p05.build_stop_readiness(_sd(), _diag())["tracks"]["real_sample"]
        self.assertEqual(t["status"], "INFORMATIONAL")
        self.assertEqual(t["total_closed"], 12)
        self.assertEqual(t["stops"], 5)
        self.assertEqual(t["slippage_coverage_pct"], 83.3)
        self.assertIn("nunca são somados", t["note"])
        self.assertFalse(t["gates_p052c"])
        self.assertFalse(t["ready"])

    def test_telemetria_sozinha_nao_libera_p052c(self):
        diag = _diag(path_status="USABLE", lat_status="USABLE")
        out = p05.build_stop_readiness(_sd(), diag)
        self.assertTrue(out["tracks"]["forward_path"]["ready"])
        self.assertTrue(out["tracks"]["live_execution"]["ready"])
        self.assertEqual(out["state"], "COLLECTING")
        self.assertFalse(out["ready_for_p052c"])

    def test_falha_de_trilha_nao_derruba_as_demais(self):
        def _boom(*a, **k):
            raise RuntimeError("trilha quebrou")

        with patch.object(p05, "_track_forward_path", _boom):
            out = p05.build_stop_readiness(_sd(), _diag())
        self.assertEqual(out["tracks"]["forward_path"]["status"], "UNAVAILABLE")
        self.assertFalse(out["tracks"]["forward_path"]["ready"])
        self.assertIn("trilha indisponível", out["tracks"]["forward_path"]["reason"])
        self.assertEqual(out["tracks"]["stop_pattern"]["phase"], "P05.2A")
        self.assertEqual(out["state"], "COLLECTING")

    def test_blocos_ausentes_nao_quebram(self):
        out = p05.build_stop_readiness(_sd(real=None), {"mae_mfe": None,
                                                        "telemetry": None})
        self.assertEqual(out["tracks"]["forward_path"]["status"], "UNAVAILABLE")
        self.assertEqual(out["tracks"]["live_execution"]["status"], "UNAVAILABLE")
        self.assertIn(out["state"], p05.P052R_STATES)


# ════════════════════════════════════════════════════════════════════════════
#  ETA
# ════════════════════════════════════════════════════════════════════════════
class Eta(unittest.TestCase):

    def _insufficient(self, span=120.0, removed=4):
        lab = _lab(p05.LAB_INSUFFICIENT,
                   rejected=[_candidate(p05.LAB_INSUFFICIENT, removed=removed)])
        return p05.build_stop_readiness(
            _sd(patterns=[_pattern()], lab=lab, span=span), _diag())["eta"]

    def test_menos_de_sete_dias_devolve_null(self):
        eta = self._insufficient(span=3.0)
        self.assertIsNone(eta["eta_days"])
        self.assertIn("dias observados", eta["eta_reason"])
        self.assertEqual(p05.P051R_MIN_OBSERVED_DAYS_FOR_RATE, 7)

    def test_taxa_zero_devolve_null(self):
        eta = self._insufficient(removed=0)
        self.assertEqual(eta["daily_rate"], 0.0)
        self.assertIsNone(eta["eta_days"])

    def test_arredondamento_para_cima(self):
        eta = self._insufficient(span=100.0, removed=10)
        self.assertIsNotNone(eta["eta_days"])
        self.assertIsInstance(eta["eta_days"], int)
        self.assertGreater(eta["eta_days"], 0)

    def test_missing_nunca_negativo(self):
        # afetadas acima do mínimo → missing 0, nunca negativo
        eta = self._insufficient(removed=999)
        self.assertEqual(eta["eta_days"], 0)
        self.assertGreaterEqual(eta["eta_days"], 0)

    def test_hipotese_rejeitada_devolve_null(self):
        lab = _lab(p05.LAB_REJECTED, rejected=[_candidate(p05.LAB_REJECTED)])
        eta = p05.build_stop_readiness(
            _sd(patterns=[_pattern()], lab=lab), _diag())["eta"]
        self.assertIsNone(eta["eta_days"])
        self.assertIn("contrariada", eta["eta_reason"])

    def test_collecting_devolve_null_com_motivo(self):
        eta = p05.build_stop_readiness(_sd(), _diag())["eta"]
        self.assertIsNone(eta["eta_days"])
        self.assertIn("não é previsível", eta["eta_reason"])

    def test_ready_devolve_zero(self):
        lab = _lab(p05.LAB_VALIDATION_SUPPORTED, candidates=[_candidate()])
        eta = p05.build_stop_readiness(
            _sd(patterns=[_pattern()], lab=lab), _diag())["eta"]
        self.assertEqual(eta["eta_days"], 0)

    def test_eta_reason_sempre_presente(self):
        cenarios = [
            p05.build_stop_readiness({}, {}),
            p05.build_stop_readiness(_sd(), _diag()),
            p05.build_stop_readiness(
                _sd(patterns=[_pattern()], lab=_lab(p05.LAB_INSUFFICIENT)), _diag()),
            p05.build_stop_readiness(
                _sd(patterns=[_pattern()], lab=_lab(p05.LAB_REJECTED)), _diag()),
        ]
        for out in cenarios:
            self.assertTrue(out["eta"]["eta_reason"], out["state"])
            self.assertIn("eta_days", out["eta"])


# ════════════════════════════════════════════════════════════════════════════
#  Holdout selado e zero escrita
# ════════════════════════════════════════════════════════════════════════════
class HoldoutEEscrita(unittest.TestCase):

    def test_contrato_declara_holdout_selado(self):
        out = p05.build_stop_readiness(_sd(), _diag())
        self.assertEqual(out["holdout_status"], "SEALED")
        self.assertFalse(out["holdout_outcomes_read"])
        self.assertFalse(out["holdout_metrics_computed"])

    def test_sentinela_do_holdout_nunca_e_acessada(self):
        sd = _sd(patterns=[_pattern()],
                 lab=_lab(p05.LAB_VALIDATION_SUPPORTED, candidates=[_candidate()]))
        sd["test_rows"] = [_SealedRow({"id": i}) for i in range(10)]
        out = p05.build_stop_readiness(sd, _diag())
        self.assertEqual(out["state"], "READY_FOR_P052C")
        for row in sd["test_rows"]:
            with self.assertRaises(AssertionError):
                _ = row["realized_r"]

    def test_zero_metrica_do_teste(self):
        out = p05.build_stop_readiness(_sd(), _diag())
        blob = repr(out)
        for proibido in ("'realized_r'", "test_expectancy", "test_stop_rate",
                         "test_metrics", "holdout_expectancy"):
            self.assertNotIn(proibido, blob)
        # o monitor NÃO lê o split do teste: nenhuma LINHA DE CÓDIGO o referencia
        src = (BACKEND / "services" / "strategy_evidence_service.py").read_text()
        bloco = src.split("P05.2R — STOP EVIDENCE READINESS MONITOR")[-1].split(
            "async def build_diagnosis")[0]
        codigo = "\n".join(ln for ln in bloco.splitlines()
                           if not ln.lstrip().startswith("#"))
        for proibido in ('split["test"]', 'get("test")', '["realized_r"]',
                         'get("realized_r")', "load_stop_shadow_split",
                         "_partition_outcomes", "compute_evidence_metrics"):
            self.assertNotIn(proibido, codigo, proibido)

    def test_sessao_falsa_falha_se_houver_escrita(self):
        class _NoWriteSession:
            def __getattr__(self, name):
                if name in ("add", "commit", "flush", "merge", "delete",
                            "execute", "add_all"):
                    raise AssertionError(f"ESCRITA PROIBIDA no P05.2R: {name}")
                raise AttributeError(name)

        class _Ctx:
            async def __aenter__(self):
                return _NoWriteSession()

            async def __aexit__(self, *a):
                return False

        import db
        with patch.object(db, "get_session", lambda: _Ctx()):
            out = p05.build_stop_readiness(
                _sd(patterns=[_pattern()],
                    lab=_lab(p05.LAB_VALIDATION_SUPPORTED,
                             candidates=[_candidate()])), _diag())
        self.assertEqual(out["state"], "READY_FOR_P052C")

    def test_monitor_e_puro_nao_e_corrotina(self):
        self.assertFalse(asyncio.iscoroutinefunction(p05.build_stop_readiness))


# ════════════════════════════════════════════════════════════════════════════
#  Arquitetura
# ════════════════════════════════════════════════════════════════════════════
class Arquitetura(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.svc = (BACKEND / "services" / "strategy_evidence_service.py").read_text()
        cls.block = cls.svc.split("P05.2R — STOP EVIDENCE READINESS MONITOR")[-1].split(
            "async def build_diagnosis")[0]

    def test_zero_escrita(self):
        for proibido in ("session.add", "session.commit", "session.flush",
                         "session.merge", "session.delete", "update(",
                         "StrategyExperiment"):
            self.assertNotIn(proibido, self.block, proibido)

    def test_zero_rede_sdk_ou_exchange(self):
        for proibido in ("import ccxt", "exchange_service", "shadow_trade_service",
                         "place_order", "requests.", "aiohttp", "httpx", "urllib",
                         "get_session"):
            self.assertNotIn(proibido, self.block, proibido)

    def test_zero_scheduler_worker_ou_loop(self):
        for proibido in ("asyncio.create_task", "while True", "schedule",
                         "APScheduler", "Queue(", "Thread("):
            self.assertNotIn(proibido, self.block, proibido)

    def test_zero_notificacao_externa(self):
        for proibido in ("push_service", "notify_alert", "whatsapp", "smtp",
                         "sendgrid", "email"):
            self.assertNotIn(proibido, self.block.lower().replace("_email", ""))

    def test_zero_env_ou_flag_nova(self):
        self.assertNotIn("os.getenv", self.block)
        env = p05.env_snapshot()
        self.assertFalse([k for k in env if "P052R" in k.upper()])

    def test_reutiliza_contratos_existentes(self):
        for contrato in ("estimate_eta", "P051_MIN_AFFECTED",
                         "P05_MIN_FEATURE_COVERAGE_PCT", "P052B_ALLOWED_AXES",
                         "P052B_MIN_PRESERVED_PCT", "P051T_MIN_OBSERVED",
                         "P051R_MIN_OBSERVED_DAYS_FOR_RATE", "HOLDOUT_SEALED",
                         "STOP_PERSISTENT_ADVERSE", "LAB_VALIDATION_SUPPORTED"):
            self.assertIn(contrato, self.block, contrato)

    def test_sem_endpoint_novo(self):
        """Nenhuma ROTA nova: procurado em declarações, não em prosa."""
        main = (BACKEND / "main.py").read_text()
        rotas = [ln for ln in main.splitlines()
                 if ln.lstrip().startswith("@app.")]
        for proibido in ("stop-readiness", "p05.2r", "open-holdout", "retry-now",
                         "p05/readiness-monitor"):
            self.assertFalse([r for r in rotas if proibido in r.lower()], proibido)
        # e o P05.2R nem sequer é citado no roteador
        self.assertNotIn("build_stop_readiness", main)

    def test_integrado_ao_status_existente(self):
        status = self.svc.split("async def get_p05_status")[-1].split(
            "\nasync def ")[0]
        self.assertIn("build_stop_readiness", status)
        self.assertIn('out["stop_readiness"]', status)

    def test_integrado_ao_agregado_do_painel(self):
        src = (BACKEND / "services" / "assertiveness_service.py").read_text()
        self.assertIn("build_stop_readiness", src)
        self.assertIn('out["stop_readiness"]', src)

    def test_sem_cache_paralelo(self):
        self.assertNotIn("_CACHE", self.block)
        self.assertNotIn("time.monotonic", self.block)

    def test_status_permitidos_declarados(self):
        self.assertEqual(set(p05.P052R_STATES), {
            "UNAVAILABLE", "COLLECTING", "INSUFFICIENT_EVIDENCE",
            "HYPOTHESIS_REJECTED", "READY_FOR_P052C"})


class Frontend(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        panel = (BACKEND.parent / "frontend" / "src" / "components"
                 / "AssertivenessPanel.tsx")
        cls.src = panel.read_text()
        cls.block = cls.src.split("P05.2R Prontidão para a próxima etapa")[-1].split(
            "P05.1 Qualidade da evidência")[0]

    def test_secao_existe(self):
        self.assertIn("Prontidão para a próxima etapa", self.src)

    def test_textos_obrigatorios(self):
        self.assertIn("Pronto para P05.2C não significa aprovado", self.block)
        self.assertIn("O holdout final continua fechado", self.block)
        self.assertIn("Nenhuma alteração foi aplicada à estratégia", self.block)
        self.assertIn("Telemetria suficiente não prova causalidade", self.block)
        self.assertIn("🔒 Holdout protegido", self.block)

    def test_sem_botao_ou_acao(self):
        for proibido in ("<button", "onClick", "Aplicar", "Promover", "Ativar",
                         "Abrir holdout", "Executar", "Testar agora"):
            self.assertNotIn(proibido, self.block, proibido)

    def test_mostra_trilhas_eta_e_proxima_acao(self):
        self.assertIn("Padrões de stop", self.src)
        self.assertIn("Laboratório offline", self.src)
        self.assertIn("Trajetória MAE/MFE", self.src)
        self.assertIn("Latência da entrada real", self.src)
        self.assertIn("Amostra real", self.src)
        self.assertIn("Previsão:", self.block)
        self.assertIn("Próxima ação:", self.block)

    def test_nunca_renderiza_objeto(self):
        # nenhuma interpolação direta de objeto/dicionário na seção
        self.assertNotIn("{t}", self.block)
        self.assertNotIn("{rd.tracks}", self.block)
        self.assertNotIn("{rd.eta}", self.block)

    def test_dist_nao_foi_editado(self):
        dist = BACKEND.parent / "frontend" / "dist" / "assets"
        if dist.exists():
            for f in dist.glob("*.js"):
                self.assertNotIn("Prontidão para a próxima etapa",
                                 f.read_text(errors="ignore"))


if __name__ == "__main__":
    unittest.main()
