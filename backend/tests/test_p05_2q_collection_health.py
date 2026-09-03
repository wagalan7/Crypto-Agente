"""P05.2Q — guarda de saúde da coleta prospectiva de evidência do P05.

Suíte HERMÉTICA: rede/DNS bloqueados e CONTABILIZADOS. Sem banco, exchange,
Telegram real ou credencial. O holdout final permanece SELADO.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

# ── Hermeticidade ───────────────────────────────────────────────────────────
import socket as _socket

_REAL_GETADDRINFO = _socket.getaddrinfo
_REAL_CREATE_CONNECTION = _socket.create_connection
_NET_ATTEMPTS: list = []


def _blocked_net(*a, **k):
    _NET_ATTEMPTS.append(a[:1])
    raise RuntimeError(f"REDE BLOQUEADA no teste P05.2Q (hermético): {a[:1]}")


def setUpModule():
    _NET_ATTEMPTS.clear()
    _socket.getaddrinfo = _blocked_net
    _socket.create_connection = _blocked_net


def tearDownModule():
    _socket.getaddrinfo = _REAL_GETADDRINFO
    _socket.create_connection = _REAL_CREATE_CONNECTION
    if _NET_ATTEMPTS:
        raise RuntimeError(f"HERMETICIDADE VIOLADA: {_NET_ATTEMPTS} tentativa(s) de rede.")


import main                                                  # noqa: E402
from services import strategy_evidence_service as p05        # noqa: E402

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
_health = p05.summarize_collection_health
_fmt = main._fmt_p052q_collection_health_digest

RET_OK = {"retention_at_risk": False, "retention_warning": None,
          "history_status": "YOUNG_HISTORY", "observed_retention_days": 40.0}
RET_RISK = {"retention_at_risk": True, "retention_warning": "política curta",
            "history_status": "MATURE", "observed_retention_days": 130.0}


def _row(*, hours_ago=1, path=True, mae=0.3, mfe=0.8, regime="TREND",
         zone="limit_ob", resolved_at="auto", idx=0):
    feats = {}
    if regime is not None:
        feats["regime"] = regime
    if zone is not None:
        feats["entry_zone_type"] = zone
    if path:
        feats["p05_path"] = {"status": "FINAL_OBSERVED", "mae_r": mae, "mfe_r": mfe}
    ts = NOW - timedelta(hours=hours_ago) if resolved_at == "auto" else resolved_at
    return {"id": idx, "dedupe_key": f"snap:{idx}", "features": feats,
            "created_at": ts, "resolved_at": ts}


def _cohort(n, **kw):
    return [_row(idx=i, **kw) for i in range(n)]


def _real(n, *, linked=None):
    linked = n if linked is None else linked
    out = [{"recommendation_id": i + 1} for i in range(linked)]
    out += [{"recommendation_id": None} for _ in range(n - linked)]
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Coortes e estados
# ════════════════════════════════════════════════════════════════════════════
class Coortes(unittest.TestCase):

    def test_coorte_24h_saudavel(self):
        h = _health(_cohort(10), retention=RET_OK, real_rows=_real(5), now=NOW)
        self.assertEqual(h["status"], "HEALTHY")
        self.assertEqual(h["sample_window"], "24h")
        self.assertEqual(h["last_24h"]["eligible_resolved"], 10)
        self.assertEqual(h["last_24h"]["path_present"], 10)
        self.assertEqual(h["last_24h"]["path_presence_pct"], 100.0)
        self.assertEqual(h["last_24h"]["mae_mfe_coverage_pct"], 100.0)
        self.assertEqual(h["last_24h"]["regime_coverage_pct"], 100.0)
        self.assertEqual(h["last_24h"]["entry_zone_type_coverage_pct"], 100.0)
        self.assertEqual(h["blockers"], [])

    def test_fallback_para_7d(self):
        rows = _cohort(2) + [_row(idx=90 + i, hours_ago=60) for i in range(6)]
        h = _health(rows, retention=RET_OK, now=NOW)
        self.assertEqual(h["status"], "HEALTHY")
        self.assertEqual(h["sample_window"], "7d")
        self.assertEqual(h["last_24h"]["eligible_resolved"], 2)
        self.assertEqual(h["last_7d"]["eligible_resolved"], 8)

    def test_historico_antigo_nao_contamina_coorte_recente(self):
        antigos = [_row(idx=500 + i, hours_ago=24 * 40, path=False,
                        regime=None, zone=None) for i in range(200)]
        h = _health(_cohort(10) + antigos, retention=RET_OK, now=NOW)
        self.assertEqual(h["status"], "HEALTHY")
        self.assertEqual(h["sample_window"], "24h")
        self.assertEqual(h["last_7d"]["eligible_resolved"], 10)

    def test_warming_up(self):
        for n in (1, 2, 3, 4):
            h = _health(_cohort(n), retention=RET_OK, now=NOW)
            self.assertEqual(h["status"], "WARMING_UP", n)
            self.assertEqual(h["sample_window"], "7d")
            self.assertEqual(h["blockers"], [])

    def test_idle_nunca_vira_stalled(self):
        for rows in ([], [_row(idx=1, hours_ago=24 * 30)]):
            h = _health(rows, retention=RET_OK, now=NOW)
            self.assertIn(h["status"], ("IDLE", "WARMING_UP"))
            self.assertNotEqual(h["status"], "STALLED")
        h = _health([], retention=RET_OK, now=NOW)
        self.assertEqual(h["status"], "IDLE")
        self.assertEqual(h["reason_code"], "NO_RECENT_RESOLUTIONS")

    def test_stalled_exige_coorte_julgavel_e_zero_paths(self):
        h = _health(_cohort(8, path=False), retention=RET_OK, now=NOW)
        self.assertEqual(h["status"], "STALLED")
        self.assertEqual(h["blockers"], ["path_presence"])
        self.assertEqual(h["last_24h"]["path_present"], 0)
        # com menos de 5, nunca STALLED
        self.assertEqual(_health(_cohort(4, path=False), retention=RET_OK,
                                 now=NOW)["status"], "WARMING_UP")

    def test_path_presente_com_mae_mfe_ausente_degrada(self):
        rows = _cohort(8)
        for r in rows[:4]:
            r["features"]["p05_path"] = {"status": "COLLECTING", "mae_r": None,
                                         "mfe_r": None,
                                         "unavailable_reason": "sem candles"}
        h = _health(rows, retention=RET_OK, now=NOW)
        self.assertEqual(h["status"], "DEGRADED")
        self.assertIn("mae_mfe", h["blockers"])
        self.assertEqual(h["last_24h"]["path_presence_pct"], 100.0)
        self.assertEqual(h["last_24h"]["mae_mfe_coverage_pct"], 50.0)
        self.assertIn("sem candles", h["last_24h"]["missing_by_reason"])

    def test_mae_mfe_nao_finito_nao_conta(self):
        rows = _cohort(8)
        for r in rows[:5]:
            r["features"]["p05_path"] = {"status": "X", "mae_r": float("nan"),
                                         "mfe_r": float("inf")}
        h = _health(rows, retention=RET_OK, now=NOW)
        self.assertEqual(h["status"], "DEGRADED")
        self.assertEqual(h["last_24h"]["mae_mfe_observed"], 3)

    def test_baixa_cobertura_de_regime(self):
        rows = _cohort(10)
        for r in rows[:4]:
            r["features"].pop("regime")
        h = _health(rows, retention=RET_OK, now=NOW)
        self.assertEqual(h["status"], "DEGRADED")
        self.assertIn("regime", h["blockers"])
        self.assertEqual(h["last_24h"]["regime_coverage_pct"], 60.0)

    def test_baixa_cobertura_de_entry_zone(self):
        rows = _cohort(10)
        for r in rows[:5]:
            r["features"]["entry_zone_type"] = "   "
        h = _health(rows, retention=RET_OK, now=NOW)
        self.assertEqual(h["status"], "DEGRADED")
        self.assertIn("entry_zone_type", h["blockers"])

    def test_cobertura_exatamente_no_piso_passa(self):
        rows = _cohort(10)
        for r in rows[:2]:
            r["features"].pop("regime")
        h = _health(rows, retention=RET_OK, now=NOW)
        self.assertEqual(h["last_24h"]["regime_coverage_pct"], 80.0)
        self.assertEqual(h["status"], "HEALTHY")
        self.assertEqual(p05.P05_MIN_FEATURE_COVERAGE_PCT, 80.0)


# ════════════════════════════════════════════════════════════════════════════
#  Retenção e REAL
# ════════════════════════════════════════════════════════════════════════════
class RetencaoEReal(unittest.TestCase):

    def test_retencao_em_risco_degrada(self):
        h = _health(_cohort(10), retention=RET_RISK, now=NOW)
        self.assertEqual(h["status"], "DEGRADED")
        self.assertEqual(h["reason_code"], "RETENTION_AT_RISK")
        self.assertEqual(h["blockers"], ["retention"])

    def test_retencao_indisponivel_vira_unavailable(self):
        for ret in (None, "x", [], {"error": "loader falhou"}):
            h = _health(_cohort(10), retention=ret, now=NOW)
            self.assertEqual(h["status"], "UNAVAILABLE", repr(ret))
            self.assertEqual(h["sample_window"], None)

    def test_rows_invalidas_viram_unavailable(self):
        for rows in (None, "x", 42, {"a": 1}):
            h = _health(rows, retention=RET_OK, now=NOW)
            self.assertEqual(h["status"], "UNAVAILABLE", repr(rows))

    def test_real_vazio_e_informativo(self):
        h = _health(_cohort(10), retention=RET_OK, real_rows=[], now=NOW)
        self.assertEqual(h["status"], "HEALTHY")
        self.assertEqual(h["real_linkage"]["total_real"], 0)
        self.assertIsNone(h["real_linkage"]["link_coverage_pct"])
        self.assertFalse(h["real_linkage"]["gates_health"])
        self.assertNotIn("real_linkage", h["blockers"])

    def test_real_abaixo_do_minimo_nao_degrada(self):
        h = _health(_cohort(10), retention=RET_OK,
                    real_rows=_real(4, linked=0), now=NOW)
        self.assertEqual(h["status"], "HEALTHY")
        self.assertEqual(h["real_linkage"]["link_coverage_pct"], 0.0)
        self.assertFalse(h["real_linkage"]["gates_health"])

    def test_real_com_cinco_e_vinculo_baixo_degrada(self):
        h = _health(_cohort(10), retention=RET_OK,
                    real_rows=_real(5, linked=2), now=NOW)
        self.assertEqual(h["status"], "DEGRADED")
        self.assertIn("real_linkage", h["blockers"])
        self.assertEqual(h["real_linkage"]["link_coverage_pct"], 40.0)
        self.assertEqual(p05.P052Q_MIN_REAL_FOR_GATE, 5)

    def test_vinculo_exige_recommendation_id_valido(self):
        rows = [{"recommendation_id": 1}, {"recommendation_id": "abc"},
                {"recommendation_id": None}, {"recommendation_id": "  "},
                {"recommendation_id": True}, {}]
        h = _health(_cohort(10), retention=RET_OK, real_rows=rows, now=NOW)
        self.assertEqual(h["real_linkage"]["total_real"], 6)
        self.assertEqual(h["real_linkage"]["linked_to_recommendation"], 2)

    def test_real_nao_le_lucro_nem_outcome(self):
        rows = [{"recommendation_id": 1, "realized_r": 99.0, "pnl_usd": 500,
                 "status": "closed_stop", "direction": "long", "score": 70}] * 5
        h = _health(_cohort(10), retention=RET_OK, real_rows=rows, now=NOW)
        blob = repr(h)
        for proibido in ("realized_r", "pnl_usd", "99.0", "closed_stop",
                         "expectancy", "win_rate"):
            self.assertNotIn(proibido, blob, proibido)


# ════════════════════════════════════════════════════════════════════════════
#  Timestamps e tolerância
# ════════════════════════════════════════════════════════════════════════════
class TimestampsETolerancia(unittest.TestCase):

    def test_timestamps_invalidos_e_futuros_sao_excluidos(self):
        rows = _cohort(8) + [
            _row(idx=800, resolved_at=NOW + timedelta(hours=3)),
            _row(idx=801, resolved_at=None),
            _row(idx=802, resolved_at="2026-09-01"),
            _row(idx=803, resolved_at=12345),
        ]
        h = _health(rows, retention=RET_OK, now=NOW)
        self.assertEqual(h["last_24h"]["eligible_resolved"], 8)
        self.assertEqual(h["excluded_by_reason"]
                         ["timestamp ausente, inválido ou futuro"], 4)

    def test_timestamp_naive_e_tratado_como_utc(self):
        naive = (NOW - timedelta(hours=2)).replace(tzinfo=None)
        rows = _cohort(7) + [_row(idx=900, resolved_at=naive)]
        h = _health(rows, retention=RET_OK, now=NOW)
        self.assertEqual(h["last_24h"]["eligible_resolved"], 8)
        self.assertTrue(h["last_24h"]["last_resolved_at"].endswith("+00:00"))

    def test_determinismo_com_now_injetavel(self):
        rows = _cohort(10)
        a = _health(rows, retention=RET_OK, real_rows=_real(5), now=NOW)
        b = _health(rows, retention=RET_OK, real_rows=_real(5), now=NOW)
        self.assertEqual(a, b)

    def test_payload_malformado_nao_lanca(self):
        rows = [None, "x", 42, {"features": "x", "resolved_at": NOW},
                {"features": None, "resolved_at": NOW},
                {"resolved_at": NOW, "features": {"p05_path": "x"}}]
        h = _health(rows, retention=RET_OK, real_rows=["x", None, 7], now=NOW)
        self.assertIn(h["status"], p05.P052Q_STATES)
        self.assertEqual(h["real_linkage"]["total_real"], 0)

    def test_ausencia_nunca_vira_zero(self):
        h = _health([], retention=RET_OK, now=NOW)
        self.assertIsNone(h["last_24h"]["path_presence_pct"])
        self.assertIsNone(h["last_24h"]["mae_mfe_coverage_pct"])
        self.assertIsNone(h["last_24h"]["last_resolved_at"])

    def test_contrato_de_saida(self):
        h = _health(_cohort(10), retention=RET_OK, real_rows=_real(5), now=NOW)
        for chave in ("phase", "read_only", "status", "reason_code", "detail",
                      "sample_window", "thresholds", "last_24h", "last_7d",
                      "real_linkage", "retention", "blockers", "computed_at",
                      "affects_strategy"):
            self.assertIn(chave, h, chave)
        self.assertEqual(h["phase"], "P05.2Q")
        self.assertTrue(h["read_only"])
        self.assertFalse(h["affects_strategy"])

    def test_sem_ids_outcomes_ou_payload_completo(self):
        rows = _cohort(10)
        rows[0]["id"] = 987654
        rows[0]["dedupe_key"] = "snap:987654"
        h = _health(rows, retention=RET_OK, now=NOW)
        blob = repr(h)
        for proibido in ("987654", "dedupe_key", "created_at", "FINAL_OBSERVED"):
            self.assertNotIn(proibido, blob, proibido)


# ════════════════════════════════════════════════════════════════════════════
#  Holdout, escrita e arquitetura
# ════════════════════════════════════════════════════════════════════════════
class Arquitetura(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.svc = (BACKEND / "services" / "strategy_evidence_service.py").read_text()
        cls.block = cls.svc.split("P05.2Q — EVIDENCE COLLECTION HEALTH GUARD")[-1] \
            .split("async def build_telemetry_section")[0]
        cls.main = (BACKEND / "main.py").read_text()

    def test_loader_nunca_seleciona_realized_r(self):
        loader = self.svc.split("async def _load_readiness_rows")[1].split(
            "\nasync def ")[0]
        self.assertNotIn("RS.realized_r", loader)
        self.assertNotIn('"realized_r"', loader.split('"""')[2] if '"""' in loader
                         else loader)

    @staticmethod
    def _codigo(bloco: str) -> str:
        """Só as linhas de CÓDIGO: comentários e docstrings ficam de fora."""
        linhas = []
        dentro = False
        for ln in bloco.splitlines():
            aspas = ln.count('"""')
            if dentro:
                if aspas:
                    dentro = False
                continue
            if ln.lstrip().startswith("#"):
                continue
            if aspas == 1:
                dentro = True
                continue
            if aspas >= 2:
                ln = ln.split('"""')[0]
            linhas.append(ln)
        return "\n".join(linhas)

    def test_health_guard_nunca_acessa_outcome_nem_holdout(self):
        codigo = self._codigo(self.block)
        for proibido in ('"realized_r"', "'realized_r'", "temporal_split",
                         "load_stop_shadow_split", 'split["test"]',
                         '["test"]', "holdout", "outcome_class",
                         "_partition_outcomes", "SNAP_RESOLVED"):
            self.assertNotIn(proibido, codigo, proibido)

    def test_nao_calcula_metrica_de_resultado(self):
        codigo = self._codigo(self.block)
        for proibido in ("win_rate", "expectancy", "profit_factor",
                         "compute_evidence_metrics", "bootstrap",
                         "wilson_interval", "max_drawdown", "realized"):
            self.assertNotIn(proibido, codigo, proibido)

    def test_zero_escrita_rede_ou_exchange(self):
        for proibido in ("session.add", "session.commit", "session.flush",
                         "session.merge", "session.delete", "get_session",
                         "select(", "update(", "StrategyExperiment",
                         "exchange_service", "requests.", "aiohttp", "httpx",
                         "send_telegram", "notification_service"):
            self.assertNotIn(proibido, self.block, proibido)

    def test_funcao_e_pura_e_sincrona(self):
        self.assertNotIn("await ", self.block)
        self.assertNotIn("async def", self.block)

    def test_nao_altera_stop_readiness_nem_ready(self):
        for proibido in ("stop_readiness", "ready_for_p052c", "build_stop_readiness",
                         "READY_FOR_P052C"):
            self.assertNotIn(proibido, self.block, proibido)

    def test_reutiliza_contratos_existentes(self):
        for contrato in ("context_value", "CONTEXT_UNKNOWN", "_finite",
                         "P05_MIN_FEATURE_COVERAGE_PCT"):
            self.assertIn(contrato, self.block, contrato)

    def test_loader_reutilizado_uma_vez_sem_query_nova(self):
        secao = self.svc.split("async def build_telemetry_section")[1].split(
            "\n# ═")[0]
        self.assertIn("summarize_collection_health(", secao)
        self.assertIn("readiness_rows, retention=out.get(\"retention\"), "
                      "real_rows=real_rows", secao)
        # nenhum loader extra dentro da chamada do health guard
        chamada = secao.split("summarize_collection_health(")[1].split(")")[0]
        self.assertNotIn("_load_", chamada)
        self.assertNotIn("await", chamada)

    def test_nenhuma_env_tabela_endpoint_ou_loop_novo(self):
        self.assertNotIn("os.getenv", self.block)
        env = p05.env_snapshot()
        self.assertFalse([k for k in env if "P052Q" in k.upper()])
        for proibido in ("CREATE TABLE", "alembic", "asyncio.create_task",
                         "while True"):
            self.assertNotIn(proibido, self.block, proibido)
        rotas = [ln for ln in self.main.splitlines() if ln.lstrip().startswith("@app.")]
        self.assertFalse([r for r in rotas if "collection-health" in r.lower()
                          or "p05.2q" in r.lower()])

    def test_arquivos_proibidos_intactos(self):
        """O P05.2Q não alterou os arquivos proibidos DA SUA FASE.

        Verificado sobre o commit da fase (`ad844219..8bba744c`), não sobre o
        working tree: o R05B altera `shadow_trade_service` com autorização
        explícita.
        """
        import subprocess
        proibidos = ["backend/services/snapshot_service.py",
                     "backend/services/notification_service.py",
                     "backend/services/shadow_trade_service.py",
                     "backend/models", "backend/db.py", "frontend/src"]
        for caminho in proibidos:
            res = subprocess.run(
                ["git", "diff", "--name-only", "ad844219", "8bba744c", "--", caminho],
                cwd=BACKEND.parent, capture_output=True, text=True)
            if res.returncode != 0:
                self.skipTest("commits da fase P05.2Q indisponíveis neste checkout")
            self.assertEqual(res.stdout.strip(), "",
                             f"P05.2Q alterou arquivo proibido: {caminho}")


# ════════════════════════════════════════════════════════════════════════════
#  Formatador do Telegram
# ════════════════════════════════════════════════════════════════════════════
class FormatadorTelegram(unittest.TestCase):

    def test_healthy(self):
        out = _fmt({"status": "HEALTHY", "sample_window": "24h",
                    "last_24h": {"path_present": 42, "mae_mfe_coverage_pct": 95.2},
                    "real_linkage": {"total_real": 5, "link_coverage_pct": 100.0}})
        self.assertEqual(out, [
            "✅ Coleta P05 saudável",
            "• 24h: 42 trajetórias · cobertura 95,2%",
            "• Contextos essenciais completos",
            "• Vínculo REAL: 100% (5 operações)",
        ])

    def test_healthy_com_janela_7d(self):
        out = _fmt({"status": "HEALTHY", "sample_window": "7d",
                    "last_7d": {"path_present": 8, "mae_mfe_coverage_pct": 100.0},
                    "real_linkage": {"total_real": 0, "link_coverage_pct": None}})
        self.assertIn("• 7d: 8 trajetórias · cobertura 100,0%", out)
        self.assertFalse(any("Vínculo REAL" in ln for ln in out))

    def test_warming_up_idle_stalled(self):
        self.assertEqual(_fmt({"status": "WARMING_UP"}), [
            "🟡 Coleta P05 aquecendo",
            "• Ainda há poucas recomendações resolvidas para julgar a coleta"])
        self.assertEqual(_fmt({"status": "IDLE"}), [
            "⚪ Coleta P05 sem novas resoluções",
            "• Não há evidência de falha; apenas não houve amostra recente"])
        self.assertEqual(_fmt({"status": "STALLED"}), [
            "🔴 Coleta P05 possivelmente parada",
            "• Existem recomendações resolvidas, mas nenhuma trajetória foi registrada",
            "• Verificar o resolver de telemetria"])

    def test_degraded_traduz_bloqueios(self):
        out = _fmt({"status": "DEGRADED",
                    "blockers": ["mae_mfe", "regime", "codigo_interno", None, 7]})
        self.assertEqual(out[0], "⚠️ Coleta P05 degradada")
        self.assertIn("• MAE/MFE incompletos", out)
        self.assertIn("• contexto de regime incompleto", out)
        self.assertEqual(out[-1], "• Nenhuma estratégia foi alterada")
        texto = "\n".join(out)
        for proibido in ("codigo_interno", "mae_mfe", "None", "7"):
            self.assertNotIn(proibido, texto, proibido)

    def test_unavailable_e_desconhecido(self):
        esperado = ["⚠️ Saúde da coleta P05 indisponível",
                    "• Não foi possível confirmar a integridade da coleta"]
        for h in ({"status": "UNAVAILABLE"}, {}, None, "x", [], 42,
                  {"status": "INVENTADO"}, {"status": None}):
            self.assertEqual(_fmt(h), esperado, repr(h))

    def test_nunca_imprime_none_nan_inf_ou_objeto(self):
        cenarios = [
            {"status": "HEALTHY", "sample_window": "24h",
             "last_24h": {"path_present": None, "mae_mfe_coverage_pct": float("nan")},
             "real_linkage": {"total_real": float("inf"), "link_coverage_pct": None},
             "detail": {"obj": 1}, "reason_code": "SEGREDO"},
            {"status": "DEGRADED", "blockers": None, "error": "traceback interno"},
        ]
        for h in cenarios:
            texto = "\n".join(_fmt(h))
            for proibido in ("None", "nan", "inf", "[object Object]", "{", "}",
                             "SEGREDO", "traceback interno"):
                self.assertNotIn(proibido, texto, f"{proibido} em {h}")

    def test_bloco_curto_e_sem_markdown_perigoso(self):
        for h in ({"status": "HEALTHY", "sample_window": "24h",
                   "last_24h": {"path_present": 999, "mae_mfe_coverage_pct": 100.0},
                   "real_linkage": {"total_real": 50, "link_coverage_pct": 100.0}},
                  {"status": "DEGRADED", "blockers": list(p05.P052Q_BLOCKERS)},
                  {"status": "STALLED"}, {}):
            linhas = _fmt(h)
            self.assertLessEqual(len(linhas), 5)
            texto = "\n".join(linhas)
            self.assertLess(len(texto), 400)
            for char in ("*", "_", "`", "[", "]"):
                self.assertNotIn(char, texto, f"{char} em {h}")

    def test_helper_e_puro(self):
        src = (BACKEND / "main.py").read_text()
        block = src.split("def _fmt_p052q_collection_health_digest")[1].split(
            "\ndef _fmt_digest")[0]
        for proibido in ("await ", "async ", "get_session", "send_telegram",
                         "notification_service", "requests", "aiohttp",
                         "get_assertiveness", "summarize_collection_health",
                         "exchange_service"):
            self.assertNotIn(proibido, block, proibido)
        self.assertIn("except Exception", block)


# ════════════════════════════════════════════════════════════════════════════
#  Integração com o digest existente
# ════════════════════════════════════════════════════════════════════════════
class IntegracaoDigest(unittest.TestCase):

    def _a(self, health=None):
        p05_block = {"stop_readiness": {"state": "COLLECTING"}}
        if health is not None:
            p05_block["telemetry"] = {"collection_health": health}
        return {
            "enabled": True, "window_days": 30,
            "equity_curve": {"final_cum_r": 1.0, "final_cum_pnl_usd": 5.0,
                             "max_drawdown_r": 0.2, "current_streak": 1,
                             "points": []},
            "real_money": {"count": 0},
            "p05": p05_block,
        }

    def test_bloco_aparece_uma_vez_apos_p052n(self):
        texto = main._fmt_digest(self._a({"status": "IDLE"}))
        self.assertEqual(texto.count("⚪ Coleta P05 sem novas resoluções"), 1)
        self.assertLess(texto.index("🟡 P05.2: coletando evidências"),
                        texto.index("⚪ Coleta P05 sem novas resoluções"))

    def test_digest_preserva_o_resto(self):
        texto = main._fmt_digest(self._a({"status": "IDLE"}))
        self.assertIn("📊 *Digest diário*", texto)
        self.assertIn("• Equity:", texto)
        self.assertIn("🟡 P05.2: coletando evidências", texto)

    def test_health_ausente_nao_quebra(self):
        for a in (self._a(), {"enabled": True, "p05": None}, {},
                  {"p05": {"telemetry": "x"}}):
            texto = main._fmt_digest(a)
            self.assertIsInstance(texto, str)
            self.assertIn("⚠️ Saúde da coleta P05 indisponível", texto)

    def test_um_unico_envio_diario(self):
        src = (BACKEND / "main.py").read_text()
        loop = src.split("async def _daily_digest_loop")[1].split("\nasync def ")[0]
        self.assertEqual(loop.count("send_telegram("), 1)
        self.assertEqual(loop.count("_fmt_digest("), 1)
        self.assertIn('event_type="digest"', loop)
        self.assertIn('parse_mode="Markdown"', loop)
        self.assertNotIn("_fmt_p052q_collection_health_digest", loop)

    def test_helper_chamado_uma_vez_no_fmt_digest(self):
        src = (BACKEND / "main.py").read_text()
        bloco = src.split("def _fmt_digest(")[1].split("\nasync def ")[0]
        self.assertEqual(bloco.count("_fmt_p052q_collection_health_digest("), 1)
        self.assertEqual(bloco.count("_fmt_p052_readiness_digest("), 1)


if __name__ == "__main__":
    unittest.main()
