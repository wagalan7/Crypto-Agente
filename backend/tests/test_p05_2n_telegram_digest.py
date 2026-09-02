"""P05.2N — bloco de prontidão (P05.2R) no digest diário do Telegram.

Suíte HERMÉTICA: rede/DNS bloqueados e CONTABILIZADOS. Nenhuma mensagem real é
enviada, nenhum banco, exchange ou credencial é acessado.
"""
from __future__ import annotations

import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

# ── Hermeticidade ───────────────────────────────────────────────────────────
import socket as _socket

_REAL_GETADDRINFO = _socket.getaddrinfo
_REAL_CREATE_CONNECTION = _socket.create_connection
_NET_ATTEMPTS: list = []


def _blocked_net(*a, **k):
    _NET_ATTEMPTS.append(a[:1])
    raise RuntimeError(f"REDE BLOQUEADA no teste P05.2N (hermético): {a[:1]}")


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

_fmt = main._fmt_p052_readiness_digest


def _ready(**kw):
    base = {
        "state": "READY_FOR_P052C",
        "ready_for_p052c": True,
        "holdout_status": "SEALED",
        "holdout_outcomes_read": False,
        "holdout_metrics_computed": False,
    }
    base.update(kw)
    return base


PRONTO = "🚨 P05.2C PRONTO PARA AUDITORIA MANUAL"


# ════════════════════════════════════════════════════════════════════════════
#  Estados
# ════════════════════════════════════════════════════════════════════════════
class Estados(unittest.TestCase):

    def test_collecting_completo(self):
        out = _fmt({"state": "COLLECTING",
                    "tracks": {"forward_path": {"observed": 35, "coverage_pct": 1.6}},
                    "blocked_by": ["stop_pattern", "offline_lab"]})
        self.assertEqual(out, [
            "🟡 P05.2: coletando evidências",
            "• Telemetria futura: 35 observações · cobertura 1,6%",
            "• Aguardando padrão persistente e hipótese offline",
            "• Nenhuma alteração foi aplicada à estratégia",
        ])

    def test_collecting_com_campos_ausentes(self):
        out = _fmt({"state": "COLLECTING"})
        self.assertEqual(out[0], "🟡 P05.2: coletando evidências")
        self.assertEqual(out[-1], "• Nenhuma alteração foi aplicada à estratégia")
        self.assertFalse(any("Telemetria futura" in ln for ln in out))
        self.assertFalse(any("Aguardando" in ln for ln in out))

    def test_collecting_ignora_numeros_invalidos(self):
        for ruim in (None, "35", float("nan"), float("inf"), -1, True):
            out = _fmt({"state": "COLLECTING",
                        "tracks": {"forward_path": {"observed": ruim,
                                                    "coverage_pct": ruim}}})
            self.assertFalse(any("Telemetria futura" in ln for ln in out), ruim)

    def test_collecting_nao_vaza_nome_interno(self):
        out = _fmt({"state": "COLLECTING",
                    "blocked_by": ["stop_pattern", "eixo_desconhecido", 7, None]})
        texto = "\n".join(out)
        self.assertIn("padrão persistente", texto)
        self.assertNotIn("stop_pattern", texto)
        self.assertNotIn("eixo_desconhecido", texto)
        self.assertNotIn("None", texto)

    def test_insufficient_com_eta_valida(self):
        out = _fmt({"state": "INSUFFICIENT_EVIDENCE", "eta": {"eta_days": 12}})
        self.assertEqual(out, [
            "🟠 P05.2: evidência ainda insuficiente",
            "• Existe uma hipótese, mas a amostra ainda não permite julgamento",
            "• ETA: 12 dias",
        ])

    def test_insufficient_eta_zero_e_valida(self):
        out = _fmt({"state": "INSUFFICIENT_EVIDENCE", "eta": {"eta_days": 0}})
        self.assertIn("• ETA: 0 dias", out)

    def test_eta_invalida_nunca_e_exibida(self):
        for ruim in (None, -3, float("nan"), float("inf"), float("-inf"),
                     "12", [], {}, True):
            out = _fmt({"state": "INSUFFICIENT_EVIDENCE", "eta": {"eta_days": ruim}})
            self.assertIn("• ETA ainda indisponível", out, repr(ruim))
            self.assertFalse(any(ln.startswith("• ETA: ") for ln in out), repr(ruim))

    def test_eta_bloco_ausente(self):
        out = _fmt({"state": "INSUFFICIENT_EVIDENCE"})
        self.assertIn("• ETA ainda indisponível", out)
        out2 = _fmt({"state": "INSUFFICIENT_EVIDENCE", "eta": "quebrado"})
        self.assertIn("• ETA ainda indisponível", out2)

    def test_hypothesis_rejected(self):
        out = _fmt({"state": "HYPOTHESIS_REJECTED"})
        self.assertEqual(out, [
            "⛔ P05.2: hipótese reprovada na validação",
            "• A hipótese não deve ser forçada nem aprovada por mais amostra",
            "• Champion permanece inalterado",
        ])

    def test_unavailable(self):
        out = _fmt({"state": "UNAVAILABLE"})
        self.assertEqual(out, [
            "⚠️ P05.2: monitor indisponível",
            "• Nenhuma conclusão de prontidão foi emitida",
            "• A estratégia permanece inalterada",
        ])

    def test_estado_desconhecido(self):
        for rd in ({}, {"state": "APPROVED"}, {"state": ""}, {"state": 7},
                   {"state": None}, {"outro": 1}):
            self.assertEqual(_fmt(rd), ["⚪ P05.2: status ainda não disponível"], rd)


# ════════════════════════════════════════════════════════════════════════════
#  READY — fail-closed
# ════════════════════════════════════════════════════════════════════════════
class ReadyFailClosed(unittest.TestCase):

    def test_ready_com_todas_as_invariantes(self):
        out = _fmt(_ready())
        self.assertEqual(out, [
            PRONTO,
            "• Uma hipótese foi apoiada na validação",
            "• 🔒 Holdout final continua selado",
            "• Nenhuma regra foi ativada",
            "• Próximo passo: auditoria manual antes de qualquer alteração",
        ])

    def test_invariante_ausente_nunca_imprime_pronto(self):
        for chave in ("ready_for_p052c", "holdout_status",
                      "holdout_outcomes_read", "holdout_metrics_computed"):
            rd = _ready()
            rd.pop(chave)
            out = _fmt(rd)
            self.assertNotIn(PRONTO, out, chave)
            self.assertEqual(out[0], "⚠️ P05.2: prontidão não confirmada", chave)

    def test_invariante_divergente_nunca_imprime_pronto(self):
        divergencias = [
            {"ready_for_p052c": False}, {"ready_for_p052c": None},
            {"ready_for_p052c": "True"}, {"ready_for_p052c": 1},
            {"holdout_status": "OPEN"}, {"holdout_status": None},
            {"holdout_outcomes_read": True}, {"holdout_outcomes_read": None},
            {"holdout_outcomes_read": 0}, {"holdout_metrics_computed": True},
            {"holdout_metrics_computed": None}, {"holdout_metrics_computed": 0},
        ]
        for div in divergencias:
            out = _fmt(_ready(**div))
            self.assertNotIn(PRONTO, out, div)
            self.assertEqual(out, [
                "⚠️ P05.2: prontidão não confirmada",
                "• O contrato de segurança está incompleto ou divergente",
                "• Nenhuma alteração deve ser executada",
            ], div)

    def test_pronto_menciona_holdout_selado(self):
        texto = "\n".join(_fmt(_ready()))
        self.assertIn("🔒 Holdout final continua selado", texto)
        self.assertIn("auditoria manual", texto)
        self.assertNotIn("aprovad", texto.lower())


# ════════════════════════════════════════════════════════════════════════════
#  Tolerância a payload
# ════════════════════════════════════════════════════════════════════════════
class Tolerancia(unittest.TestCase):

    def test_payloads_malformados_nao_lancam(self):
        for ruim in (None, "texto", [], [1, 2], 42, 3.14, True, set(),
                     {"state": {"x": 1}}, {"tracks": "x"}, {"blocked_by": "x"},
                     {"state": "COLLECTING", "tracks": None, "blocked_by": None},
                     {"state": "COLLECTING", "tracks": {"forward_path": "x"}}):
            out = _fmt(ruim)
            self.assertIsInstance(out, list, repr(ruim))
            self.assertTrue(all(isinstance(ln, str) for ln in out), repr(ruim))

    def test_nunca_imprime_none_nan_inf_ou_objeto(self):
        cenarios = [
            {"state": "COLLECTING", "detail": {"x": 1}, "reason_code": "SEGREDO",
             "tracks": {"forward_path": {"observed": None, "coverage_pct": None}},
             "blocked_by": [None]},
            {"state": "INSUFFICIENT_EVIDENCE", "detail": "texto interno",
             "eta": {"eta_days": float("nan"), "eta_reason": "motivo interno"}},
            _ready(detail={"obj": 1}),
            {"state": "UNAVAILABLE", "error": "traceback interno"},
        ]
        for rd in cenarios:
            texto = "\n".join(_fmt(rd))
            for proibido in ("None", "nan", "inf", "[object Object]", "{", "}",
                             "SEGREDO", "texto interno", "motivo interno",
                             "traceback interno"):
                self.assertNotIn(proibido, texto, f"{proibido} em {rd}")

    def test_bloco_e_curto(self):
        for rd in ({"state": "COLLECTING",
                    "tracks": {"forward_path": {"observed": 999999,
                                                "coverage_pct": 100.0}},
                    "blocked_by": ["stop_pattern", "offline_lab"]},
                   _ready(), {"state": "UNAVAILABLE"}):
            linhas = _fmt(rd)
            self.assertLessEqual(len(linhas), 5)
            self.assertLess(len("\n".join(linhas)), 400)

    def test_sem_markdown_perigoso(self):
        # o digest usa parse_mode="Markdown": nenhum caractere de marcação solto
        for rd in ({"state": "COLLECTING",
                    "tracks": {"forward_path": {"observed": 35, "coverage_pct": 1.6}},
                    "blocked_by": ["stop_pattern"]},
                   {"state": "INSUFFICIENT_EVIDENCE", "eta": {"eta_days": 5}},
                   {"state": "HYPOTHESIS_REJECTED"}, {"state": "UNAVAILABLE"},
                   _ready(), {}):
            texto = "\n".join(_fmt(rd))
            for char in ("*", "_", "`", "[", "]"):
                self.assertNotIn(char, texto, f"{char} em {rd}")


# ════════════════════════════════════════════════════════════════════════════
#  Integração com o digest existente
# ════════════════════════════════════════════════════════════════════════════
class IntegracaoDigest(unittest.TestCase):

    def _a(self, readiness=None):
        return {
            "enabled": True, "window_days": 30,
            "equity_curve": {"final_cum_r": 1.5, "final_cum_pnl_usd": 20.0,
                             "max_drawdown_r": 0.5, "current_streak": 2,
                             "points": []},
            "real_money": {"count": 0},
            "p05": {"stop_readiness": readiness} if readiness is not None else {},
        }

    def test_bloco_aparece_exatamente_uma_vez(self):
        texto = main._fmt_digest(self._a({"state": "COLLECTING"}))
        self.assertEqual(texto.count("🟡 P05.2: coletando evidências"), 1)
        self.assertEqual(texto.count("• Nenhuma alteração foi aplicada à estratégia"), 1)

    def test_digest_preserva_as_linhas_originais(self):
        a = self._a({"state": "UNAVAILABLE"})
        texto = main._fmt_digest(a)
        self.assertIn("📊 *Digest diário*", texto)
        self.assertIn("• Equity:", texto)
        self.assertIn("• Real: sem trades resolvidos na janela", texto)
        # o bloco P05.2 vem no FIM, sem substituir nada
        self.assertTrue(texto.rstrip().endswith("• A estratégia permanece inalterada"))

    def test_digest_sem_p05_nao_quebra(self):
        texto = main._fmt_digest(self._a())
        self.assertIn("⚪ P05.2: status ainda não disponível", texto)
        texto2 = main._fmt_digest({"enabled": True, "p05": None})
        self.assertIn("⚪ P05.2: status ainda não disponível", texto2)
        texto3 = main._fmt_digest({})
        self.assertIsInstance(texto3, str)

    def test_digest_com_pronto(self):
        texto = main._fmt_digest(self._a(_ready()))
        self.assertEqual(texto.count(PRONTO), 1)

    def test_um_unico_envio_diario(self):
        src = (BACKEND / "main.py").read_text()
        loop = src.split("async def _daily_digest_loop")[1].split("\nasync def ")[0]
        self.assertEqual(loop.count("send_telegram("), 1)
        self.assertEqual(loop.count("_fmt_digest("), 1)
        self.assertIn('event_type="digest"', loop)
        self.assertIn('parse_mode="Markdown"', loop)
        self.assertIn("last_sent_date = now.date()", loop)
        self.assertIn("DIGEST_HOUR_UTC", loop)
        # o helper NÃO é chamado dentro do loop: só dentro de _fmt_digest
        self.assertNotIn("_fmt_p052_readiness_digest", loop)

    def test_helper_chamado_uma_vez_no_fmt_digest(self):
        src = (BACKEND / "main.py").read_text()
        bloco = src.split("def _fmt_digest(")[1].split("\nasync def ")[0]
        self.assertEqual(bloco.count("_fmt_p052_readiness_digest("), 1)


# ════════════════════════════════════════════════════════════════════════════
#  Arquitetura
# ════════════════════════════════════════════════════════════════════════════
class Arquitetura(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        src = (BACKEND / "main.py").read_text()
        cls.block = src.split("def _fmt_p052_readiness_digest")[1].split(
            "\ndef _fmt_digest")[0]
        cls.src = src

    def test_helper_e_puro(self):
        for proibido in ("await ", "async ", "get_session", "session.", "select(",
                         "send_telegram", "notification_service", "requests",
                         "aiohttp", "httpx", "get_assertiveness",
                         "build_stop_readiness", "exchange_service", "place_order"):
            self.assertNotIn(proibido, self.block, proibido)

    def test_helper_nunca_lanca(self):
        self.assertIn("except Exception", self.block)
        self.assertIn("return [\"⚪ P05.2: status ainda não disponível\"]",
                      self.block.replace("'", '"'))

    def test_nenhum_loop_endpoint_env_ou_tabela_novos(self):
        self.assertEqual(self.src.count("async def _daily_digest_loop"), 1)
        self.assertEqual(self.src.count("_digest_task = asyncio.create_task"), 1)
        for proibido in ('getenv("P052N', "getenv('P052N",
                         '@app.get("/api/strategy/p05/digest',
                         '@app.post("/api/strategy/p05/digest',
                         "CREATE TABLE", "alembic"):
            self.assertNotIn(proibido, self.src, proibido)
        rotas = [ln for ln in self.src.splitlines() if ln.lstrip().startswith("@app.")]
        self.assertFalse([r for r in rotas if "p05.2n" in r.lower()
                          or "readiness-digest" in r.lower()])

    def test_nenhuma_acao_estrategica(self):
        for proibido in ("promote", "activate", "open_holdout", "StrategyExperiment",
                         "commit(", "session.add", "create_task"):
            self.assertNotIn(proibido, self.block, proibido)

    def test_arquivos_proibidos_intactos(self):
        """O P05.2N não alterou serviços, frontend `src`, modelos ou banco.

        A verificação é feita CAMINHO A CAMINHO desde a baseline, para não
        confundir arquivo sujo preexistente do usuário (ex.: `frontend/dist`)
        com alteração desta fase.
        """
        import subprocess
        proibidos = [
            "backend/services/strategy_evidence_service.py",
            "backend/services/assertiveness_service.py",
            "backend/services/notification_service.py",
            "backend/services/shadow_trade_service.py",
            "backend/services/snapshot_service.py",
            "backend/models",
            "backend/db.py",
            "frontend/src",
        ]
        for caminho in proibidos:
            res = subprocess.run(
                ["git", "diff", "--name-only", "36a05fc3", "--", caminho],
                cwd=BACKEND.parent, capture_output=True, text=True)
            if res.returncode != 0:
                self.skipTest("baseline 36a05fc3 indisponível neste checkout")
            self.assertEqual(res.stdout.strip(), "",
                             f"arquivo proibido alterado: {caminho}")


if __name__ == "__main__":
    unittest.main()
