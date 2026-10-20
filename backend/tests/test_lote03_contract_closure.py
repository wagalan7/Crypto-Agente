"""Lote 03 §2 — fechamento dos contratos ANTES de ligar os callers.

Cada teste aqui nasceu VERMELHO contra a implementação local parcial:

  1. seletor com UMA interpretação (LEGACY default, OFF documentado como
     paridade legada, CANDIDATE inválido BLOQUEIA sem voltar ao champion);
  2. identificador do experimento validado/convertido UMA vez — ENV string
     nunca chega a uma comparação de inteiro;
  3. campo aditivo da recomendação DECLARADO (serialização legada intacta);
  4. identidade ESTÁVEL do despacho (experimento, bundle, aprovação, geração,
     modelo e decisão congelada) — `authority_hash`/leitura temporal não é
     identidade;
  5. retomada preserva a decisão ORIGINAL e reconfere validade atual;
  6. validação FECHADA de geometria/lado/símbolo/TF/preços/score/probabilidade,
     mesmo quando o adulterador recalcula o hash.
"""
import asyncio
import copy
import os
import socket
import unittest
from unittest.mock import patch

from services import entry_intent_service as intents
from services import live_candidate_adapter_service as adapter
from services import operational_governance_service as governance
from services import recommendation_service as scanner
from services import score_v3_calibration_service as c
from tests.test_lote02_calibration_final_boundaries import NOW
from tests.test_lote03_candidate_adapter import authority, decision, FEATURES


def contexto(**changes):
    result = decision(**changes)
    assert result["ok"], result
    return result["context"]


def reread(context, *, observed_at_ms=None, fence=None):
    """Mesma autoridade, OUTRA leitura: muda carimbo/fence/hash, não o fato."""
    novo = copy.deepcopy(context)
    auth = novo["authority"]
    auth["observed_at_ms"] = observed_at_ms or (int(auth.get("observed_at_ms") or NOW) + 1234)
    auth["local_fence"] = fence if fence is not None else int(auth.get("local_fence") or 0) + 1
    auth["authority_hash"] = governance.digest(
        {k: v for k, v in auth.items() if k != "authority_hash"})
    novo["selection_hash"] = adapter._hash({k: v for k, v in novo.items()
                                            if k != "selection_hash"})
    return novo


class SeletorUnico(unittest.TestCase):
    """UMA interpretação do seletor nos dois serviços."""

    def test_adapter_e_governanca_concordam_em_todo_valor(self):
        casos = {"LEGACY": "LEGACY", "legacy": "LEGACY", "OFF": "OFF",
                 "off": "OFF", "": "LEGACY", "typo": "INVALID"}
        for valor, esperado in casos.items():
            with patch.dict(os.environ, {adapter.SELECTOR_ENV: valor}):
                self.assertEqual(governance.selected_mode(), esperado, valor)
                self.assertEqual(adapter.selected_mode(), governance.selected_mode(),
                                 f"{valor}: interpretação divergente")

    def test_candidate_sem_id_valido_e_invalido_nos_dois(self):
        for bruto in ("", "0", "-1", "true", " 7", "7x", "7.0"):
            with patch.dict(os.environ, {adapter.SELECTOR_ENV: "CANDIDATE",
                                         adapter.EXPERIMENT_ENV: bruto}):
                self.assertEqual(governance.selected_mode(), "INVALID", bruto)
                self.assertEqual(adapter.selected_mode(), "INVALID", bruto)
                view = asyncio.run(adapter.load_operational_view(None))
                self.assertFalse(view["ok"], bruto)
                self.assertEqual(view["reason_code"], "OPERATIONAL_SELECTOR_INVALID")

    def test_off_e_paridade_legada_sem_consultar_autoridade(self):
        """OFF tem significado ÚNICO: nenhuma consulta e nenhuma entrada nova
        bloqueada — exatamente o comportamento LEGACY."""
        with patch.dict(os.environ, {adapter.SELECTOR_ENV: "OFF"}), \
                patch.object(governance, "load_view",
                             side_effect=AssertionError("OFF não consulta autoridade")):
            view = asyncio.run(adapter.load_operational_view(
                lambda: (_ for _ in ()).throw(AssertionError("sem sessão"))))
        self.assertTrue(view["ok"])
        self.assertEqual(view["mode"], "OFF")
        self.assertIs(adapter.candidate_mode_active(view), False)

    def test_candidate_invalido_bloqueia_sem_fallback_permissivo(self):
        with patch.dict(os.environ, {adapter.SELECTOR_ENV: "CANDIDATE",
                                     adapter.EXPERIMENT_ENV: "7"}), \
                patch.object(governance, "load_view",
                             return_value={"ok": False, "blocked": True,
                                           "reason_code": "HUMAN_APPROVAL_MISSING_OR_REVOKED"}):
            view = asyncio.run(adapter.load_operational_view(lambda: None))
        self.assertFalse(view["ok"])
        self.assertEqual(view["reason_code"], "HUMAN_APPROVAL_MISSING_OR_REVOKED")
        self.assertIs(adapter.candidate_mode_active(view), False)


class IdentificadorDoExperimento(unittest.TestCase):
    """ENV é validado/convertido UMA vez; string nunca vira comparação int."""

    def test_adapter_nao_passa_id_textual_para_a_governanca(self):
        capturado = {}

        async def espiao(factory, **kwargs):
            capturado.update(kwargs)
            return {"ok": True, "mode": "CANDIDATE"}

        with patch.dict(os.environ, {adapter.SELECTOR_ENV: "CANDIDATE",
                                     adapter.EXPERIMENT_ENV: "7"}), \
                patch.object(governance, "load_view", side_effect=espiao):
            view = asyncio.run(adapter.load_operational_view(lambda: None))
        self.assertTrue(view["ok"], view)
        self.assertNotIsInstance(capturado.get("experiment_id"), str,
                                "ID textual quebra a comparação de inteiro")
        self.assertIn(capturado.get("experiment_id"), (7, None))

    def test_governanca_recusa_id_que_nao_e_inteiro_positivo(self):
        for bruto, esperado in (("7", 7), ("12", 12)):
            with patch.dict(os.environ, {adapter.EXPERIMENT_ENV: bruto}):
                self.assertEqual(governance._selected_id(), esperado)
        for bruto in ("", "0", "-3", "true", "7 ", "0x7", "७"):
            with patch.dict(os.environ, {adapter.EXPERIMENT_ENV: bruto}):
                self.assertIsNone(governance._selected_id(), bruto)


class CampoAditivoDaRecomendacao(unittest.TestCase):
    """O campo é declarado no modelo; o legado não ganha chave nova."""

    def test_modelo_declara_o_campo_e_preserva_o_legado(self):
        from services.recommendation_service import Recommendation
        self.assertIn("operational_selection", Recommendation.model_fields)
        from tests.test_lote02_preselection_capture import sinal
        legacy = scanner._build_recommendation(sinal(timeframe="1h", conf=90), 90, "A")
        serializado = legacy.model_dump()
        self.assertNotIn("operational_selection", serializado)
        legacy.operational_selection = contexto()
        com_candidata = legacy.model_dump()
        self.assertIn("operational_selection", com_candidata)
        # Todos os campos antigos continuam presentes, sem renome/alias perdido.
        self.assertEqual(set(serializado) | {"operational_selection"},
                         set(com_candidata))


class IdentidadeEstavelDoDespacho(unittest.TestCase):
    """Leitura temporal prova validade; identidade é o fato congelado."""

    def test_duas_leituras_da_mesma_autoridade_tem_a_mesma_identidade(self):
        primeira = contexto()
        segunda = reread(primeira)
        self.assertNotEqual(primeira["selection_hash"], segunda["selection_hash"])
        self.assertNotEqual(primeira["authority"]["authority_hash"],
                            segunda["authority"]["authority_hash"])
        self.assertEqual(adapter.identity_of(primeira), adapter.identity_of(segunda))
        self.assertTrue(adapter.same_identity(primeira, segunda))

    def test_identidade_nao_inclui_carimbo_nem_fence(self):
        identidade = adapter.identity_of(contexto())
        texto = repr(identidade)
        for proibido in ("observed_at_ms", "local_fence", "authority_hash"):
            self.assertNotIn(proibido, texto, proibido)
        for exigido in ("experiment_id", "bundle_hash", "approval_id",
                        "generation", "model_fingerprint"):
            self.assertIn(exigido, identidade, exigido)

    def test_identidade_muda_quando_o_fato_muda(self):
        base = contexto()
        for campo, valor in (("experiment_id", 999), ("generation", 42)):
            outro = copy.deepcopy(base)
            outro["authority"][campo] = valor
            self.assertNotEqual(adapter.identity_of(base), adapter.identity_of(outro),
                                campo)
        aprovacao = copy.deepcopy(base)
        aprovacao["authority"]["approval"]["approval_id"] = "c" * 64
        self.assertNotEqual(adapter.identity_of(base), adapter.identity_of(aprovacao))
        self.assertFalse(adapter.same_identity(base, aprovacao))


class RetomadaPreservaADecisao(unittest.TestCase):
    """Readmitir não troca a candidata pela leitura mais recente."""

    def test_proposta_releitura_mantem_o_contexto_original(self):
        original = contexto()
        nova_leitura = reread(original)
        self.assertTrue(intents.candidate_identity_matches(original, nova_leitura))
        escolhido = intents.candidate_context_to_keep(original, nova_leitura)
        self.assertEqual(escolhido, original,
                         "a decisão congelada é a do primeiro despacho")

    def test_outra_candidata_na_readmissao_e_recusada(self):
        original = contexto()
        outra = copy.deepcopy(original)
        outra["authority"]["approval"]["approval_id"] = "d" * 64
        outra["selection_hash"] = adapter._hash(
            {k: v for k, v in outra.items() if k != "selection_hash"})
        self.assertFalse(intents.candidate_identity_matches(original, outra))
        self.assertIsNone(intents.candidate_context_to_keep(original, outra))

    def test_proposta_congelada_aceita_releitura_e_guarda_a_original(self):
        original = contexto()
        proposta = dict(account_ref="acct", exchange="binance", intent_key="intent",
                        symbol="BTCUSDT", side="BUY", order_type="MARKET",
                        dispatch_id="cw-test", qty=2.0, created_at_ms=NOW)
        congelada = intents.freeze_dispatch_proposal(
            **proposta, operational_selection=reread(original),
            stored_operational_selection=original)
        self.assertTrue(congelada["ok"], congelada)
        self.assertEqual(congelada["operational_selection"], original)


class ValidacaoFechadaDaSelecao(unittest.TestCase):
    """Geometria, lado, símbolo, TF, preços e probabilidade são conferidos."""

    def _resealed(self, **mudancas):
        ctx = contexto()
        ctx["selection"].update(mudancas)
        ctx["selection_hash"] = adapter._hash({k: v for k, v in ctx.items()
                                               if k != "selection_hash"})
        return ctx

    def _resealed_identity(self, **mudancas):
        ctx = contexto()
        ctx["selection"]["identity"].update(mudancas)
        ctx["selection_hash"] = adapter._hash({k: v for k, v in ctx.items()
                                               if k != "selection_hash"})
        return ctx

    def test_lado_desconhecido_nao_passa(self):
        for lado in ("LONG", "buy", None, "", 1):
            with self.assertRaises(ValueError, msg=lado):
                adapter.freeze_context(self._resealed_identity(side=lado))

    def test_timeframe_fora_do_catalogo_nao_passa(self):
        for tf in ("1x", "", None, "7h"):
            with self.assertRaises(ValueError, msg=tf):
                adapter.freeze_context(self._resealed_identity(timeframe=tf))

    def test_simbolo_precisa_de_quote_conhecida(self):
        for simbolo in ("", None, "BTC", "BTCZZZ", "btcusdt "):
            with self.assertRaises(ValueError, msg=simbolo):
                adapter.freeze_context(self._resealed_identity(symbol=simbolo))

    def test_precos_nao_finitos_ou_nao_positivos_nao_passam(self):
        for campo in ("entry", "stop_loss", "tp1", "tp2"):
            for valor in (0, -1, None, float("nan"), float("inf"), True, "100"):
                with self.assertRaises(ValueError, msg=f"{campo}={valor}"):
                    adapter.freeze_context(self._resealed_identity(**{campo: valor}))

    def test_geometria_incoerente_com_o_lado_nao_passa(self):
        # long exige stop < entry < tp1 < tp2
        for mudanca in ({"stop_loss": 101.0}, {"tp1": 99.0},
                        {"tp2": 102.0}, {"tp1": 107.0}):
            with self.assertRaises(ValueError, msg=str(mudanca)):
                adapter.freeze_context(self._resealed_identity(**mudanca))
        # short exige stop > entry > tp1 > tp2 (geometria espelhada)
        curto = contexto(side="short", entry=100.0, stop_loss=102.0, tp1=97.0, tp2=94.0)
        self.assertEqual(adapter.freeze_context(curto), curto)
        with self.assertRaises(ValueError):
            quebrado = copy.deepcopy(curto)
            quebrado["selection"]["identity"]["tp1"] = 103.0
            quebrado["selection_hash"] = adapter._hash(
                {k: v for k, v in quebrado.items() if k != "selection_hash"})
            adapter.freeze_context(quebrado)

    def test_probabilidade_fora_do_intervalo_ou_de_outro_evento_nao_passa(self):
        for valor in (-0.1, 1.1, None, True, "0.8"):
            with self.assertRaises(ValueError, msg=str(valor)):
                adapter.freeze_context(self._resealed(probability=valor))
        trocado = self._resealed(probability_event=c.EVENT_TP2)
        with self.assertRaises(ValueError):
            adapter.freeze_context(trocado)   # artefato é de P(TP1)

    def test_score_abaixo_do_minimo_ou_sem_modelo_nao_passa(self):
        with self.assertRaises(ValueError):
            adapter.freeze_context(self._resealed(score=10.0))
        with self.assertRaises(ValueError):
            adapter.freeze_context(self._resealed(model_fingerprint=""))

    def test_contexto_valido_continua_passando_inteiro(self):
        ctx = contexto()
        self.assertEqual(adapter.freeze_context(ctx), ctx)
        self.assertEqual(adapter.identity_of(ctx)["symbol"], "BTCUSDT")


if __name__ == "__main__":
    unittest.main()
