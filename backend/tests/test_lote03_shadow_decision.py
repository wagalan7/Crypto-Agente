"""L03/B — decisão candidata OBSERVACIONAL (escopo CANDIDATE_SHADOW).

Prova a quebra da circularidade: a candidata decide no ciclo OFICIAL do scanner
com aprovação de propósito SHADOW, enquanto o seletor operacional continua
LEGACY/OFF — sem promoção, sem CANARY, sem ordem. A decisão é congelada ANTES
dos preços futuros, persistida na anotação/hash e comparada com a recomputação
verificável do contrato congelado. Nenhuma aprovação real, nenhuma ativação de
flag, nenhum acesso a conta: as aprovações aqui são TEST_ONLY e sintéticas.
"""
import asyncio
import copy
import os
import socket
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services import live_candidate_adapter_service as adapter
from services import preselection_observation_service as pre
from services import prospective_shadow_service as prospective
from services import recommendation_service as scanner
from services import score_v3_service as s3, score_v3_calibration_service as c
from tests.test_lote02_calibration_final_boundaries import B
from tests.test_lote03_candidate_adapter import FEATURES
from tests.test_lote03_prospective_shadow import engineering_fixture

FRACAS = {**FEATURES, 'adx': 12.0, 'volume_ratio': 0.6, 'htf_alignment_ratio': 0.0,
          'structure_quality': 0.0, 'trigger_body_ratio': 0.1,
          'trigger_follow_through_atr': 0.0, 'level_distance_atr': 2.0, 'rr_tp2': 1.2}


def pontuar(features):
    payload = s3.score(features, playbook='TREND_PULLBACK', side='long',
                       config=s3.ScoreConfig())
    assert payload['state'] == s3.STATE_OK, payload
    return payload['score']


def autoridade(*, now_ms, min_score, purpose='SHADOW', experiment_id=1,
               generation=1, approval_id='b' * 64, manifest_hash=None,
               score_config_hash='h' * 64):
    """Autoridade SINTÉTICA ancorada em `now_ms` (artefato de calibração REAL).

    Fronteira declarada: só a autoridade humana é sintética (TEST_ONLY). O motor
    V3, o ajuste e a validação fora da amostra são os oficiais.
    """
    base = int(now_ms) - 1400 * B
    cut = base + 1000 * B
    cfg = s3.ScoreConfig()
    scored = s3.score(FEATURES, playbook='TREND_PULLBACK', side='long', config=cfg)
    rows = [{'opportunity_key': f'train-{faixa}-{i}', 'score': faixa * 10 + 5.,
             'label': i < 24, 'event': c.EVENT_TP1, 'decision_ts_ms': base + B,
             'label_available_ts_ms': base + 11 * B}
            for faixa in range(10) for i in range(30)]
    fitted = c.fit_calibration(rows, event=c.EVENT_TP1, population=cfg.population,
        model_fingerprint=scored['model_fingerprint'], score_config_hash=score_config_hash,
        horizon_bars=12, bar_ms=B, censoring=c.CENSORING_RULES[0], payoff_ref='M',
        source='REPLAY', dataset_hash='d' * 64, cutoff_ms=cut, generated_at_ms=cut + B,
        valid_until_ms=int(now_ms) + 10000 * B, versions={'score': s3.SCORE_VERSION})
    assert fitted['ok'], fitted
    oos = [{'opportunity_key': f'oos-{i}', 'score': 95., 'label': i < 24,
            'event': c.EVENT_TP1, 'decision_ts_ms': cut + 2 * B,
            'label_available_ts_ms': cut + 12 * B} for i in range(30)]
    checked = c.validate_out_of_sample(fitted['artifact'], oos, now_ms=int(now_ms))
    assert checked['ok'], checked
    manifesto = {'comparison_scope': 'SELECTION_ONLY', 'candidate': {
        'score_config': cfg.as_dict(), 'score_config_hash': score_config_hash,
        'selection_rule': {'kind': 'SCORE_V3_MIN_SCORE', 'playbook': 'TREND_PULLBACK',
                           'min_score': float(min_score)}}}
    if manifest_hash is not None:
        manifesto['manifest_hash'] = manifest_hash
    return {'ok': True, 'mode': 'CANDIDATE', 'authority_hash': 'a' * 64,
            'generation': generation, 'experiment_id': experiment_id, 'purpose': purpose,
            'bundle': {'operational_semantics': dict(adapter.SEMANTICS),
                       'bundle_hash': 'e' * 64, 'manifest': manifesto,
                       'calibration_artifact': checked['artifact']},
            'approval': {'approval_id': approval_id, 'test_only': True, 'limits': {
                'symbols': ['BTCUSDT'], 'playbooks': ['TREND_PULLBACK'],
                'max_orders': 1, 'max_risk_pct': .5,
                'expires_at_ms': int(now_ms) + 600_000}}}


def entrada(timeframe, features, **mudar):
    valores = dict(features=features, symbol='BTCUSDT', side='long',
                   timeframe=timeframe, entry=100., stop_loss=98., tp1=103., tp2=106.)
    valores.update(mudar)
    return valores


class NucleoObservacional(unittest.TestCase):
    """Decisão observacional ALL-TF: mesmo núcleo, outra autoridade."""

    def setUp(self):
        for alvo, nome in ((socket, 'getaddrinfo'), (socket.socket, 'connect')):
            guarda = patch.object(alvo, nome, side_effect=AssertionError('rede proibida'))
            guarda.start()
            self.addCleanup(guarda.stop)
        self.now = int(time.time() * 1000)
        self.alto, self.baixo = pontuar(FEATURES), pontuar(FRACAS)
        self.assertLess(self.baixo, self.alto)
        self.corte = (self.alto + self.baixo) / 2
        self.auth = autoridade(now_ms=self.now, min_score=self.corte)

    def grupo(self, auth=None, extras=()):
        itens = [entrada('1h', FEATURES), entrada('4h', FRACAS),
                 entrada('15m', {}), *extras]
        return adapter.shadow_group_decision(auth or self.auth, itens, now_ms=self.now)

    def test_autoridade_operacional_nao_serve_de_observacional(self):
        canary = autoridade(now_ms=self.now, min_score=self.corte, purpose='CANARY')
        resultado = adapter.shadow_group_decision(canary, [entrada('1h', FEATURES)],
                                                  now_ms=self.now)
        self.assertFalse(resultado['ok'])
        self.assertEqual(resultado['reason_code'], 'SHADOW_AUTHORITY_UNAVAILABLE')

    def test_contexto_shadow_nunca_autoriza_operacao(self):
        decisao = adapter.candidate_decision(self.auth, now_ms=self.now,
                                             **entrada('1h', FEATURES))
        self.assertTrue(decisao['ok'], decisao)
        contexto = decisao['context']
        with self.assertRaises(ValueError) as erro:
            adapter.freeze_context(contexto)
        self.assertEqual(str(erro.exception), 'CANDIDATE_PURPOSE_MISMATCH')
        # Uso OBSERVACIONAL explícito continua possível (sem autorizar nada).
        self.assertEqual(adapter.freeze_context(contexto, require_purpose='SHADOW'),
                         contexto)

    def test_alltf_congela_escolha_rejeicao_e_unknown_no_estagio_comparado(self):
        resultado = self.grupo()
        self.assertTrue(resultado['ok'], resultado)
        grupo = resultado['group']
        estados = {linha['timeframe']: linha['state'] for linha in grupo['evaluated']}
        self.assertEqual(estados, {'1h': 'SELECTED', '4h': 'REJECTED', '15m': 'UNKNOWN'})
        self.assertEqual(grupo['selected_timeframe'], '1h')
        self.assertEqual(grupo['scope'], adapter.SHADOW_SCOPE)
        self.assertEqual(grupo['purpose'], 'SHADOW')
        self.assertEqual(grupo['evaluated_timeframes'], ['1h', '4h', '15m'])
        self.assertEqual(grupo['score'], self.alto)
        self.assertIsNotNone(grupo['group_hash'])
        # Recusa por score CONHECIDO é discordância; ausência de feature é dúvida.
        por_tf = {linha['timeframe']: linha for linha in grupo['evaluated']}
        self.assertEqual(por_tf['4h']['reason_code'], 'CANDIDATE_BELOW_MIN_SCORE')
        self.assertEqual(por_tf['4h']['score'], self.baixo)
        self.assertIsNone(por_tf['15m']['score'])

    def test_vencedor_por_maior_score_mesmo_fora_de_ordem(self):
        resultado = self.grupo(extras=[entrada('1d', FEATURES, entry=100., tp2=107.)])
        grupo = resultado['group']
        # Empate de score: a ordem congelada do runtime decide (primeiro vence).
        self.assertEqual(grupo['selected_timeframe'], '1h')
        self.assertEqual(len(grupo['evaluated']), 4)

    def test_mesma_funcao_de_decisao_do_caminho_operacional(self):
        with patch.object(adapter, 'candidate_decision',
                          wraps=adapter.candidate_decision) as espiao:
            self.grupo()
        self.assertEqual(espiao.call_count, 3)

    def test_bloco_por_timeframe_carrega_identidade_de_regra(self):
        grupo = self.grupo()['group']
        bloco = adapter.shadow_decision_for_timeframe(grupo, '4h')
        self.assertEqual(bloco['scope'], adapter.SHADOW_SCOPE)
        self.assertEqual(bloco['state'], 'REJECTED')
        self.assertFalse(bloco['selected'])
        self.assertEqual(bloco['authority']['purpose'], 'SHADOW')
        self.assertEqual(bloco['authority']['score_config_hash'], 'h' * 64)
        self.assertEqual(bloco['group']['selected_timeframe'], '1h')
        self.assertIsNone(adapter.shadow_decision_for_timeframe(grupo, '2h'))

    def test_grupo_vazio_nao_inventa_decisao(self):
        self.assertFalse(adapter.shadow_group_decision(self.auth, [], now_ms=self.now)['ok'])

    def test_expiracao_da_aprovacao_shadow_vira_unknown_nao_selecao(self):
        tarde = self.auth['approval']['limits']['expires_at_ms'] + 1
        grupo = adapter.shadow_group_decision(self.auth, [entrada('1h', FEATURES)],
                                              now_ms=tarde)['group']
        self.assertEqual(grupo['evaluated'][0]['state'], 'UNKNOWN')
        self.assertIsNone(grupo['selected_timeframe'])


class AllowlistCongelada(unittest.TestCase):
    """O congelador aceita a decisão observacional — e só a válida."""

    def setUp(self):
        self.now = int(time.time() * 1000)
        corte = (pontuar(FEATURES) + pontuar(FRACAS)) / 2
        auth = autoridade(now_ms=self.now, min_score=corte, manifest_hash='m' * 64)
        grupo = adapter.shadow_group_decision(auth, [entrada('1h', FEATURES)],
                                              now_ms=self.now)['group']
        self.bloco = adapter.shadow_decision_for_timeframe(grupo, '1h')

    def congelar(self, **mudar):
        argumentos = dict(identity='pre-abc', outcome=pre.OUTCOME_ACCEPTED,
            decision_ts_ms=self.now, setup={'symbol': 'BTCUSDT', 'timeframe': '1h',
                'side': 'long', 'playbook': 'CHAMPION_LEGACY',
                'playbook_version': 'LEGACY_V1', 'trigger_candle_ms': self.now - 1000,
                'entry': 100., 'stop_loss': 98., 'tp1': 103., 'tp2': 106., 'atr': 2.},
            funnel={}, availability={}, source={'decision_source': 'server_scan'},
            observed_decision_scope='FINAL_SCANNER_SELECTION',
            shadow_decision=self.bloco)
        argumentos.update(mudar)
        return pre.frozen_decision(**argumentos)

    def test_decisao_observacional_entra_no_payload_sem_tocar_a_final(self):
        payload = self.congelar()
        self.assertEqual(payload['schema_version'], pre.PRE_SCHEMA_VERSION_V2)
        self.assertEqual(payload['observed_decision_scope'], 'FINAL_SCANNER_SELECTION')
        shadow = payload['shadow_decision']
        self.assertEqual(shadow['scope'], pre.SHADOW_DECISION_SCOPE)
        self.assertEqual(shadow['state'], 'SELECTED')
        self.assertEqual(shadow['authority']['purpose'], 'SHADOW')
        self.assertEqual(shadow['authority']['manifest_hash'], 'm' * 64)
        self.assertEqual(shadow['timeframe'], '1h')
        # O escopo da decisão FINAL não é sobrescrito pela observação.
        self.assertNotEqual(payload['observed_decision_scope'], shadow['scope'])

    def test_escopo_candidato_de_runtime_e_preservado(self):
        payload = self.congelar(observed_decision_scope='CANDIDATE_SCANNER_SELECTION',
                                shadow_decision=None)
        self.assertEqual(payload['observed_decision_scope'], 'CANDIDATE_SCANNER_SELECTION')
        self.assertNotIn('shadow_decision', payload)

    def test_escopo_desconhecido_nao_entra(self):
        self.assertNotIn('observed_decision_scope',
                         self.congelar(observed_decision_scope='INVENTADO'))

    def test_decisao_sem_proposito_shadow_e_recusada(self):
        for mudanca in ({'purpose': 'CANARY'}, {'approval_id': ''},
                        {'experiment_id': 0}, {'generation': True}):
            with self.subTest(mudanca=mudanca):
                bloco = copy.deepcopy(self.bloco)
                bloco['authority'].update(mudanca)
                self.assertNotIn('shadow_decision', self.congelar(shadow_decision=bloco))

    def test_estado_e_escopo_invalidos_nao_viram_observacao(self):
        for campo, valor in (('state', 'OK'), ('scope', 'FINAL_SCANNER_SELECTION'),
                             ('timeframe', None)):
            with self.subTest(campo=campo):
                bloco = {**copy.deepcopy(self.bloco), campo: valor}
                self.assertNotIn('shadow_decision', self.congelar(shadow_decision=bloco))

    def test_grupo_absurdo_nao_estoura_o_orcamento(self):
        bloco = copy.deepcopy(self.bloco)
        bloco['group']['evaluated_timeframes'] = [f'{i}h' for i in range(40)]
        self.assertNotIn('shadow_decision', self.congelar(shadow_decision=bloco))

    def test_ausencia_mantem_payload_v1_identico(self):
        payload = self.congelar(shadow_decision=None, observed_decision_scope=None)
        self.assertEqual(payload['schema_version'], pre.PRE_SCHEMA_VERSION)
        self.assertNotIn('shadow_decision', payload)


class LinhaDoScanner(unittest.TestCase):
    """A linha observada do scanner carrega a decisão do PRÓPRIO timeframe."""

    def setUp(self):
        self.now = int(time.time() * 1000)
        corte = (pontuar(FEATURES) + pontuar(FRACAS)) / 2
        auth = autoridade(now_ms=self.now, min_score=corte)
        grupo = adapter.shadow_group_decision(
            auth, [entrada('1h', FEATURES), entrada('4h', FRACAS)], now_ms=self.now)['group']
        self.grupo = grupo

    def sinal(self, timeframe):
        from tests.test_lote02_preselection_capture import sinal
        return sinal(timeframe=timeframe, conf=90)

    def linha(self, timeframe, bloco_tf):
        return scanner._preselection_candidate(
            self.sinal(timeframe), 80.0, stages=[], accepted=True,
            shadow=adapter.shadow_decision_for_timeframe(self.grupo, bloco_tf))

    def test_anexa_so_a_decisao_do_timeframe_da_linha(self):
        linha = self.linha('1h', '1h')
        self.assertEqual(linha['shadow_decision']['state'], 'SELECTED')
        self.assertEqual(linha['observed_decision_scope'], 'FINAL_SCANNER_SELECTION')
        self.assertEqual(linha['config']['decision_scope'], 'FINAL_SCANNER_SELECTION')

    def test_decisao_de_outro_timeframe_nao_contamina_a_linha(self):
        self.assertNotIn('shadow_decision', self.linha('1h', '4h'))

    def test_sem_autoridade_a_linha_e_a_de_antes(self):
        self.assertNotIn('shadow_decision', self.linha('1h', '2h'))


class CicloOficialDoScanner(unittest.TestCase):
    """Ciclo OFICIAL: observa a candidata e não muda nada do champion."""

    def setUp(self):
        from services import decision_observation_service as obs
        self.obs = obs
        self.salvos = dict(obs._pending)
        obs._pending.clear()
        self.addCleanup(lambda: (obs._pending.clear(), obs._pending.update(self.salvos)))
        for alvo, nome in ((socket, 'getaddrinfo'), (socket.socket, 'connect')):
            guarda = patch.object(alvo, nome, side_effect=AssertionError('rede proibida'))
            guarda.start()
            self.addCleanup(guarda.stop)
        # A cobertura do ciclo é um acumulador de MÓDULO: rodar o scanner real
        # aqui a suja para quem lê o resumo depois. Restaura ao sair.
        self.addCleanup(pre.reset_coverage)
        self.now = int(time.time() * 1000)

    def sinais(self):
        import pandas as pd
        from tests.test_lote02_preselection_capture import sinal
        saida = {}
        for tf, aberto in (('1h', 99.0), ('4h', 99.9)):
            sig = sinal(timeframe=tf, conf=90 if tf == '1h' else 78)
            sig.current_price = 100.0
            sig.indicators.pivot_low = 99.0
            sig.data_freshness = {'candle': {'quality': 'FRESH', 'symbol': sig.symbol,
                'timeframe': tf, 'close_time_ms': self.now - 1000,
                'open_time_ms': self.now - 3_601_000, 'observed_at_ms': self.now}}
            scanner._capture_research_inputs(sig, pd.DataFrame(
                [dict(open=aberto, high=100.0, low=99.0, close=100.0)]))
            sig._r13_research_inputs.update(trigger_reference=98.0,
                                            trigger_reference_ms=self.now - 7_200_000)
            sig.mtf = {'alignment': 'bullish', 'higher_tfs': [
                {'timeframe': '1d', 'ema_aligned': 'bullish', 'data_freshness':
                    {'candle': {**sig.data_freshness['candle'], 'timeframe': '1d'}}}]}
            saida[(sig.symbol, tf)] = sig
        return saida

    def rodar(self, *, auth):
        from tests.test_lote02_capture_closure import run_scan
        with ExitStack() as pilha:
            pilha.enter_context(patch.dict(os.environ,
                {adapter.SELECTOR_ENV: 'LEGACY'}))
            pilha.enter_context(patch.object(adapter, 'load_shadow_authority',
                AsyncMock(return_value=auth)))
            return run_scan(self.sinais())

    def test_observa_a_candidata_com_seletor_legacy_e_preserva_o_champion(self):
        corte = pontuar(FEATURES) + 100.0   # nada é selecionado: corte alto
        auth = autoridade(now_ms=self.now, min_score=min(corte, 100.0))
        recs_on, linhas_on, _ = self.rodar(auth=auth)
        recs_off, linhas_off, _ = self.rodar(auth=None)
        # Champion intocado: mesma lista, mesma ordem, mesmos valores.
        def identidade(recs):
            return [(r.symbol, r.timeframe, r.direction, r.score, r.tier, r.entry,
                     r.stop_loss, r.tp2, r.leverage, r.risk_pct) for r in recs]
        self.assertEqual(identidade(recs_on), identidade(recs_off))
        self.assertTrue(recs_on)
        self.assertTrue(all(getattr(r, 'operational_selection', None) is None
                            for r in recs_on))
        self.assertEqual(len(linhas_on), len(linhas_off))
        observadas = [self.payload(linha) for linha in linhas_on]
        self.assertTrue(all('shadow_decision' in p for p in observadas), observadas)
        for p in observadas:
            self.assertEqual(p['shadow_decision']['scope'], pre.SHADOW_DECISION_SCOPE)
            self.assertEqual(p['shadow_decision']['timeframe'], p['setup']['timeframe'])
            self.assertEqual(p['shadow_decision']['authority']['purpose'], 'SHADOW')
            self.assertEqual(p['observed_decision_scope'], 'FINAL_SCANNER_SELECTION')
        # Sem a candidata ligada, nenhuma linha ganha decisão observacional.
        self.assertFalse(any('shadow_decision' in self.payload(linha)
                             for linha in linhas_off))

    def test_decisao_observacional_cobre_os_dois_timeframes_do_simbolo(self):
        corte = (pontuar(FEATURES) + pontuar(FRACAS)) / 2
        auth = autoridade(now_ms=self.now, min_score=corte)
        _, linhas, _ = self.rodar(auth=auth)
        estados = {self.payload(l)['setup']['timeframe']:
                   self.payload(l)['shadow_decision']['state'] for l in linhas}
        self.assertEqual(set(estados), {'1h', '4h'})
        self.assertIn('SELECTED', set(estados.values()))

    def payload(self, linha):
        return linha['frozen_config']['r09_pre_selection']


class AnotacaoEFidelidade(unittest.TestCase):
    """A decisão observada sobrevive à anotação e vira fidelidade medida."""

    def setUp(self):
        guarda = patch('socket.getaddrinfo', side_effect=AssertionError('rede proibida'))
        guarda.start()
        self.addCleanup(guarda.stop)
        self.exp, self.context, self.row, _ = engineering_fixture()
        self.payload = self.row['frozen_config']['r09_pre_selection']
        self.manifesto = self.context['manifest']['candidate']

    def bloco(self, estado='SELECTED', **mudar):
        base = {'scope': adapter.SHADOW_SCOPE, 'version': adapter.SHADOW_DECISION_VERSION,
                'rule': 'MAX_CANDIDATE_SCORE', 'state': estado,
                'timeframe': self.payload['setup']['timeframe'],
                'score': 80.0, 'min_score': self.manifesto['selection_rule']['min_score'],
                'probability': 0.8, 'probability_event': c.EVENT_TP1,
                'model_fingerprint': 'f' * 64, 'reason_code': None,
                'is_selected_timeframe': True, 'selected': estado == 'SELECTED',
                'decided_at_ms': self.payload['decision_ts_ms'],
                'group': {'evaluated_timeframes': [self.payload['setup']['timeframe']],
                          'selected_timeframe': self.payload['setup']['timeframe'],
                          'group_hash': 'g' * 64},
                'authority': {'experiment_id': self.context['experiment_id'],
                              'generation': self.context['generation'],
                              'approval_id': self.context['approval_id'],
                              'bundle_hash': 'e' * 64,
                              'manifest_hash': self.context['manifest_hash'],
                              'score_config_hash': self.manifesto['score_config_hash'],
                              'playbook': self.manifesto['selection_rule']['playbook'],
                              'purpose': 'SHADOW'}}
        base.update(mudar)
        vista = pre._shadow_decision_view(base)
        self.assertIsNotNone(vista, base)
        return vista

    def anotar(self, bloco=None, escopo='FINAL_SCANNER_SELECTION'):
        self.payload['observed_decision_scope'] = escopo
        if bloco is not None:
            self.payload['shadow_decision'] = bloco
        return prospective.build_preselection_annotation(self.row, self.context)

    def test_anotacao_persiste_escopo_decisao_e_identidade_de_regra(self):
        ann = self.anotar(self.bloco())
        self.assertTrue(prospective.verify_annotation(ann))
        frozen = ann['frozen']
        self.assertEqual(frozen['observed_decision_scope'], 'FINAL_SCANNER_SELECTION')
        self.assertEqual(frozen['shadow_decision']['state'], 'SELECTED')
        self.assertEqual(frozen['candidate_score_config_hash'],
                         self.manifesto['score_config_hash'])
        self.assertEqual(frozen['candidate_min_score'],
                         self.manifesto['selection_rule']['min_score'])
        # O hash cobre a decisão observada: adulterar não passa.
        adulterada = copy.deepcopy(ann)
        adulterada['frozen']['shadow_decision']['state'] = 'REJECTED'
        self.assertFalse(prospective.verify_annotation(adulterada))

    def test_sem_decisao_observada_a_anotacao_nao_inventa_uma(self):
        ann = self.anotar()
        self.assertIsNone(ann['frozen']['shadow_decision'])

    def fidelidade(self, *anotacoes):
        return prospective.fidelity_measure(list(anotacoes))

    def anotacao_com(self, estado):
        ann = self.anotar(self.bloco(estado))
        ann['frozen']['decisions']['candidate'] = {'state': 'SELECTED',
                                                   'reason_code': 'OK'}
        return ann

    def test_concordancia_e_divergencia_medidas_na_mesma_oportunidade(self):
        medida = self.fidelidade(self.anotacao_com('SELECTED'))
        self.assertEqual(medida['fidelity_comparable'], 1)
        self.assertEqual(medida['fidelity_divergences'], 0)
        self.assertEqual(medida['fidelity_discrepancy_pct'], 0.0)
        self.assertEqual(medida['fidelity_coverage_pct'], 100.0)
        self.assertIsNone(medida['fidelity_gap_reason'])
        divergente = self.fidelidade(self.anotacao_com('REJECTED'))
        self.assertEqual(divergente['fidelity_divergences'], 1)
        self.assertEqual(divergente['fidelity_discrepancy_pct'], 100.0)

    def test_unknown_nao_e_acerto_nem_divergencia(self):
        medida = self.fidelidade(self.anotacao_com('UNKNOWN'))
        self.assertEqual(medida['fidelity_comparable'], 0)
        self.assertEqual(medida['fidelity_unknown'], 1)
        self.assertIsNone(medida['fidelity_discrepancy_pct'])
        self.assertEqual(medida['fidelity_gap_reason'], prospective.FIDELITY_GAP_REASON)
        self.assertEqual(medida['fidelity_reasons'], {'OBSERVED_UNKNOWN': 1})

    def test_recomputacao_indeterminada_tambem_e_unknown(self):
        ann = self.anotar(self.bloco())
        ann['frozen']['decisions']['candidate'] = {'state': 'UNKNOWN',
            'reason_code': 'POINT_IN_TIME_FEATURES_NOT_CONFIRMED'}
        medida = self.fidelidade(ann)
        self.assertEqual(medida['fidelity_comparable'], 0)
        self.assertIsNone(medida['fidelity_discrepancy_pct'])
        self.assertEqual(medida['fidelity_reasons'], {'RECOMPUTED_UNKNOWN': 1})

    def test_hash_de_regra_divergente_e_incomparavel_nao_zero_por_cento(self):
        for campo, valor in (('score_config_hash', 'z' * 64),
                             ('manifest_hash', 'z' * 64),
                             ('generation', 999),
                             ('approval_id', 'z' * 64)):
            with self.subTest(campo=campo):
                bloco = self.bloco()
                bloco['authority'][campo] = valor
                ann = self.anotar(bloco)
                ann['frozen']['decisions']['candidate'] = {'state': 'SELECTED'}
                medida = self.fidelidade(ann)
                self.assertIsNone(medida['fidelity_discrepancy_pct'])
                self.assertEqual(medida['fidelity_unknown'], 1)
                self.assertEqual(medida['fidelity_reasons'],
                                 {'CONFIG_NOT_RECONCILED': 1})

    def test_denominador_divergente_e_incomparavel(self):
        bloco = self.bloco()
        bloco['min_score'] = (self.manifesto['selection_rule']['min_score'] or 0) + 7
        ann = self.anotar(bloco)
        ann['frozen']['decisions']['candidate'] = {'state': 'SELECTED'}
        self.assertEqual(self.fidelidade(ann)['fidelity_reasons'],
                         {'DENOMINATOR_NOT_RECONCILED': 1})

    def test_estagio_diferente_nao_e_a_mesma_decisao(self):
        bloco = self.bloco()
        bloco['timeframe'] = '15m' if self.payload['setup']['timeframe'] != '15m' else '1h'
        ann = self.anotar(bloco)
        self.assertEqual(self.fidelidade(ann)['fidelity_reasons'],
                         {'STAGE_TIMEFRAME_MISMATCH': 1})

    def test_coorte_sem_observacao_candidata_fica_com_lacuna_declarada(self):
        ann = self.anotar()
        medida = self.fidelidade(ann)
        self.assertIsNone(medida['fidelity_discrepancy_pct'])
        self.assertEqual(medida['fidelity_observed'], 0)
        self.assertEqual(medida['fidelity_reasons'],
                         {'NO_OBSERVED_CANDIDATE_DECISION': 1})
        self.assertEqual(medida['fidelity_gap_reason'], prospective.FIDELITY_GAP_REASON)

    def test_coorte_vazia_nunca_vira_fidelidade_zero(self):
        vazia = self.fidelidade()
        self.assertIsNone(vazia['fidelity_discrepancy_pct'])
        self.assertIsNone(vazia['fidelity_coverage_pct'])
        self.assertEqual(vazia['fidelity_gap_reason'], prospective.FIDELITY_GAP_REASON)
        medidas = prospective.operational_measurements([], [], {}, now_ms=1)
        self.assertIsNone(medidas['fidelity_discrepancy_pct'])
        self.assertEqual(medidas['reason_code'], 'NO_PROSPECTIVE_OBSERVATIONS')

    def test_runtime_candidato_promovido_continua_comparavel(self):
        ann = self.anotar(escopo=prospective.RUNTIME_CANDIDATE_SCOPE)
        ann['frozen']['observed_outcome'] = 'ACCEPTED'
        ann['frozen']['decisions']['candidate'] = {'state': 'REJECTED'}
        medida = self.fidelidade(ann)
        self.assertEqual(medida['fidelity_comparable'], 1)
        self.assertEqual(medida['fidelity_discrepancy_pct'], 100.0)

    def test_gate_bloqueia_quando_a_fidelidade_nao_pode_ser_medida(self):
        from services import preselection_experiment_service as r12
        ann = self.anotar()
        resumo = prospective.summarize_prospective([ann],
            started_at_ms=self.context['started_at_ms'],
            now_ms=self.context['manifest']['split']['as_of_ms'],
            enabled_playbooks=['TREND_PULLBACK'])
        self.assertIsNone(resumo['measurements']['fidelity_discrepancy_pct'])
        self.assertEqual(resumo['gate']['verdict'], 'NO_GO')
        self.assertFalse(resumo['promotable'])
        self.assertEqual(r12.GoNoGoCriteria().max_fidelity_discrepancy_pct, 5.0)


if __name__ == '__main__':
    unittest.main()
