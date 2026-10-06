"""L03/C — proteção SIMULADA do Shadow, com fonte explícita e versionada.

Substitui a inferência "economia ausente ⇒ proteção" por observação do stop
ativo REALMENTE percorrido pelo replay oficial. Nada aqui coloca ordem, lê conta
ou prova SL/TP real: o escopo é `SHADOW_SIMULATED` e isso está declarado no
próprio registro. Lucro não é prova de proteção; obrigação aberta é falha
pendente; zero só existe com observação aplicável e cobertura auditável.
"""
import copy
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services import offline_replay_service as replay
from services import prospective_shadow_service as prospective
from tests.test_lote03_prospective_shadow import engineering_fixture

BAR = 300_000
T0 = 1_700_000_100_000   # múltiplo exato de bar_ms: a 1ª vela fecha em T0


def config(**mudar):
    valores = dict(bar_ms=BAR, entry_window_bars=3, max_holding_bars=8,
                   pre_tp1_time_stop_bars=6, tp1_fraction=0.5,
                   be_lock_fraction=0.5, trail_atr_multiple=1.0,
                   trail_activation_atr=1.0)
    valores.update(mudar)
    return replay.ReplayConfig(**valores)


def custos(**mudar):
    valores = dict(fee_bps_per_side=2.0, slippage_bps_per_side=1.0,
                   funding_bps_per_bar=0.0)
    valores.update(mudar)
    return replay.CostConfig(**valores)


def oportunidade(**mudar):
    valores = dict(opportunity_id='opp-1', symbol='BTCUSDT', direction='long',
                   decision_ts_ms=T0, entry=100.0, stop_loss=98.0, tp1=103.0,
                   tp2=110.0, atr=1.0)
    valores.update(mudar)
    return replay.Opportunity(**valores)


def velas(sequencia):
    """(open, high, low, close) por barra, a partir do primeiro fechamento."""
    return [replay.Candle(timestamp_ms=T0 + i * BAR, open=o, high=h,
                          low=l, close=c, volume=10.0)
            for i, (o, h, l, c) in enumerate(sequencia)]


class MotorOficial(unittest.TestCase):
    """A trilha sai do estado percorrido — não de um resumo paralelo."""

    def rodar(self, sequencia, **mudar):
        return replay.replay_opportunity(oportunidade(**mudar), velas(sequencia),
                                         config(), custos())

    def test_fonte_declarada_versionada_e_nunca_sl_real(self):
        resultado = self.rodar([(100., 100.5, 99.5, 100.), (100., 100., 97.9, 98.)])
        protecao = resultado['protection']
        self.assertEqual(protecao['source'], 'SHADOW_SIMULATED')
        self.assertEqual(protecao['version'], replay.PROTECTION_VERSION)
        self.assertEqual(protecao['producer'], replay.PROTECTION_PRODUCER)
        self.assertIs(protecao['proves_real_sl'], False)
        self.assertEqual(protecao['config_hash'], resultado['config_hash'])
        self.assertEqual(protecao['cost_config_hash'], resultado['cost_config_hash'])
        self.assertEqual(protecao['opportunity_id'], 'opp-1')

    def test_observa_do_fill_ate_a_saida_com_stop_finito_e_geometria(self):
        resultado = self.rodar([(100., 100.5, 99.5, 100.), (100., 100.2, 97.5, 98.)])
        protecao = resultado['protection']
        self.assertEqual(resultado['status'], 'CLOSED_STOP')
        estagios = [o['stage'] for o in protecao['observations']]
        self.assertEqual(estagios[0], 'ENTRY_FILL')
        self.assertIn('EXIT', estagios)
        primeira = protecao['observations'][0]
        self.assertEqual(primeira['active_stop'], 98.0)
        self.assertTrue(primeira['stop_finite'])
        self.assertEqual(primeira['geometry'], 'LOSS_SIDE')
        self.assertTrue(primeira['geometry_valid'])
        self.assertTrue(primeira['obligation'])
        self.assertEqual(primeira['remaining_qty'], 1.0)
        self.assertEqual(protecao['obligation_opened_ts_ms'], T0)
        self.assertIs(protecao['obligation_open_at_end'], False)
        self.assertEqual(protecao['pending_failures'], 0)
        self.assertEqual(protecao['remaining_at_end'], 0.0)
        self.assertEqual(protecao['resolution_identity']['status'], 'CLOSED_STOP')

    def test_break_even_e_trail_sao_transicoes_observadas_nao_rejeitadas(self):
        # TP1 na 2ª barra, avanço forte na 3ª (ativa trail), TP2 na 4ª.
        resultado = self.rodar([(100., 100.5, 99.5, 100.),
                                (100., 103.5, 99.8, 103.),
                                (103., 105.5, 102.5, 105.),
                                (105., 110.5, 105.0, 110.)])
        protecao = resultado['protection']
        self.assertEqual(resultado['status'], 'CLOSED_TP2')
        estagios = [o['stage'] for o in protecao['observations']]
        self.assertIn('TP1_PARTIAL', estagios)
        self.assertIn('BREAK_EVEN', estagios)
        self.assertTrue(protecao['break_even_applied'])
        be = next(o for o in protecao['observations'] if o['stage'] == 'BREAK_EVEN')
        # Stop em LUCRO depois do TP1 é CORRETO e não pode ser reprovado pela
        # geometria inicial (que exigia stop do lado da perda).
        self.assertEqual(be['geometry'], 'PROFIT_LOCK')
        self.assertTrue(be['geometry_valid'])
        self.assertGreater(be['active_stop'], 100.0)
        self.assertEqual(protecao['pending_failures'], 0)
        if protecao['trail_updates']:
            trail = next(o for o in protecao['observations'] if o['stage'] == 'TRAIL')
            self.assertTrue(trail['geometry_valid'])

    def test_saida_lucrativa_sem_fechamento_ainda_exige_prova(self):
        # TP1 realizado (lucro parcial) e a janela termina com runner aberto.
        resultado = self.rodar([(100., 100.5, 99.5, 100.),
                                (100., 103.5, 99.8, 103.)])
        protecao = resultado['protection']
        self.assertEqual(resultado['status'], 'INSUFFICIENT_DATA')
        self.assertIn('INCOMPLETE_FORWARD_HORIZON', resultado['reason_codes'])
        self.assertGreater(protecao['remaining_at_end'], 0.0)
        self.assertIs(protecao['obligation_open_at_end'], True)
        self.assertEqual([f['code'] for f in protecao['failures']],
                         [replay.PROTECTION_OBLIGATION_UNRESOLVED])
        self.assertEqual(protecao['pending_failures'], 1)
        self.assertIn('WINDOW_END', [o['stage'] for o in protecao['observations']])

    def test_sem_fill_nao_inventa_obrigacao(self):
        resultado = self.rodar([(90., 91., 89., 90.), (90., 91., 89., 90.),
                               (90., 91., 89., 90.)])
        self.assertEqual(resultado['status'], 'NOT_FILLED')
        protecao = resultado['protection']
        self.assertEqual(protecao['observations'], [])
        self.assertIsNone(protecao['obligation_open_at_end'])
        self.assertIsNone(protecao['remaining_at_end'])
        self.assertEqual(protecao['failures'], [])

    def test_entrada_ambigua_mantem_obrigacao_pendente(self):
        resultado = self.rodar([(101., 104., 97., 103.)])
        self.assertEqual(resultado['status'], 'AMBIGUOUS_ENTRY_BAR')
        protecao = resultado['protection']
        self.assertIs(protecao['obligation_open_at_end'], True)
        self.assertEqual(protecao['pending_failures'], 1)

    def test_resultado_economico_nao_muda_com_a_instrumentacao(self):
        sequencia = [(100., 100.5, 99.5, 100.), (100., 103.5, 99.8, 103.),
                     (103., 110.5, 102.5, 110.)]
        resultado = self.rodar(sequencia)
        economico = {k: v for k, v in resultado.items() if k != 'protection'}
        self.assertIsNotNone(economico['net_r'])
        self.assertEqual(economico['status'], 'CLOSED_TP2')
        # A trilha é ADITIVA: nenhum campo econômico existente foi alterado.
        self.assertEqual(set(resultado) - set(economico), {'protection'})
        self.assertNotIn('protection', economico)

    def test_motor_nao_virou_executor(self):
        fonte = inspect.getsource(replay)
        for proibido in ('place_order', 'binance', 'send_telegram', 'RealTrade',
                         'set_leverage'):
            self.assertNotIn(proibido, fonte)


class ConsumidorDaCoorte(unittest.TestCase):
    """O consumidor confere escopo/fonte e conta as trilhas separadamente."""

    def setUp(self):
        guarda = patch('socket.getaddrinfo', side_effect=AssertionError('rede proibida'))
        guarda.start()
        self.addCleanup(guarda.stop)
        self.exp, self.context, self.row, self.prices = engineering_fixture()

    def resolvida(self):
        from services import research_dataset_service as ds
        ann = prospective.build_preselection_annotation(self.row, self.context)
        linha = copy.deepcopy(self.row)
        linha['frozen_config']['r09_pre_selection'][prospective.KEY] = ann
        chave = linha['opportunity_key']
        candles = [{'timestamp': c['timestamp_ms'], **{k: c[k] for k in
                   ('open', 'high', 'low', 'close', 'volume')}}
                   for c in self.prices['windows'][chave]]
        relogio = max(c['timestamp'] for c in candles) + BAR
        mudada = prospective.resolve_annotation(SimpleNamespace(**linha),
            {'candles': candles, 'as_of': ds.ms_datetime(relogio)})
        return mudada['r09_pre_selection'][prospective.KEY]

    def test_replay_oficial_entrega_a_trilha_reconciliavel(self):
        ann = self.resolvida()
        protecao = ann['resolution']['protection']
        self.assertEqual(protecao['source'], prospective.PROTECTION_SCOPE)
        self.assertEqual(protecao['config_hash'],
                         ann['frozen']['replay_config']['config_hash'])
        self.assertEqual(protecao['cost_config_hash'],
                         ann['frozen']['costs_config']['config_hash'])
        self.assertIsNone(prospective._protection_reconciled(
            protecao, ann['frozen'], ann['resolution']))

    def medir(self, *anotacoes):
        return prospective.protection_measure(list(anotacoes))

    def test_zero_falhas_exige_observacao_aplicavel_e_cobertura(self):
        ann = self.resolvida()
        self.assertIs(ann['resolution']['filled'], True)
        medida = self.medir(ann)
        self.assertEqual(medida['protection_applicable'], 1)
        self.assertEqual(medida['protection_observed'], 1)
        self.assertEqual(medida['protection_coverage_pct'], 100.0)
        self.assertEqual(medida['unresolved_protection_failures'], 0)
        self.assertIsNone(medida['protection_gap_reason'])

    def test_sem_fonte_de_protecao_nunca_vira_zero(self):
        ann = copy.deepcopy(self.resolvida())
        ann['resolution'].pop('protection')
        medida = self.medir(ann)
        self.assertIsNone(medida['unresolved_protection_failures'])
        self.assertEqual(medida['protection_reasons'],
                         {'PROTECTION_SOURCE_MISSING': 1})
        self.assertEqual(medida['protection_gap_reason'],
                         'PROTECTION_OBSERVATION_COVERAGE_INSUFFICIENT')

    def test_payload_que_apenas_se_diz_protegido_nao_passa(self):
        for campo, valor in (('source', 'REAL_EXCHANGE_SL'),
                             ('version', 'OUTRA'),
                             ('producer', 'OUTRO_EXECUTOR'),
                             ('proves_real_sl', True)):
            with self.subTest(campo=campo):
                ann = copy.deepcopy(self.resolvida())
                ann['resolution']['protection'][campo] = valor
                medida = self.medir(ann)
                self.assertIsNone(medida['unresolved_protection_failures'])
                self.assertIn(list(medida['protection_reasons'])[0],
                              ('PROTECTION_SOURCE_MISSING', 'PROTECTION_SCOPE_INVALID'))

    def test_gestao_ou_oportunidade_divergente_nao_reconcilia(self):
        for campo, valor, motivo in (
                ('config_hash', 'z' * 64, 'PROTECTION_CONFIG_NOT_RECONCILED'),
                ('cost_config_hash', 'z' * 64, 'PROTECTION_CONFIG_NOT_RECONCILED'),
                ('opportunity_id', 'outra', 'PROTECTION_OPPORTUNITY_MISMATCH')):
            with self.subTest(campo=campo):
                ann = copy.deepcopy(self.resolvida())
                ann['resolution']['protection'][campo] = valor
                self.assertEqual(self.medir(ann)['protection_reasons'], {motivo: 1})

    def test_trilha_vazia_e_resolucao_incompativel_sao_recusadas(self):
        ann = copy.deepcopy(self.resolvida())
        ann['resolution']['protection']['observations'] = []
        self.assertEqual(self.medir(ann)['protection_reasons'],
                         {'PROTECTION_TRACE_MISSING': 1})
        outra = copy.deepcopy(self.resolvida())
        outra['resolution']['protection']['resolution_identity']['status'] = 'OUTRO'
        self.assertEqual(self.medir(outra)['protection_reasons'],
                         {'PROTECTION_RESOLUTION_MISMATCH': 1})

    def test_obrigacao_aberta_conta_como_pendente(self):
        ann = copy.deepcopy(self.resolvida())
        ann['resolution']['protection']['obligation_open_at_end'] = True
        medida = self.medir(ann)
        self.assertEqual(medida['protection_applicable'], 1)
        self.assertEqual(medida['unresolved_protection_failures'], 1)

    def test_cobertura_abaixo_do_minimo_bloqueia_o_zero(self):
        boas = []
        for i in range(9):
            ann = copy.deepcopy(self.resolvida())
            ann['frozen'] = {**ann['frozen'], 'opportunity_key': f'k-{i}'}
            ann['resolution']['protection']['opportunity_id'] = f'k-{i}'
            ann['annotation_hash'] = prospective.digest(ann['frozen'])
            boas.append(ann)
        ruim = copy.deepcopy(self.resolvida())
        ruim['resolution'].pop('protection')
        dez = self.medir(*boas, ruim)
        self.assertEqual(dez['protection_coverage_pct'], 90.0)
        self.assertEqual(dez['unresolved_protection_failures'], 0)
        onze = self.medir(*boas[:8], ruim)
        self.assertLess(onze['protection_coverage_pct'], 90.0)
        self.assertIsNone(onze['unresolved_protection_failures'])

    def test_resolucao_e_economia_sao_trilhas_separadas_sem_elif(self):
        ann = copy.deepcopy(self.resolvida())
        ann['resolution'] = {**ann['resolution'], 'status': 'INVALID'}
        economia = copy.deepcopy(self.resolvida())
        economia['resolution'] = {**economia['resolution'], 'net_r': None}
        medidas = prospective.operational_measurements([ann, economia], [], {}, now_ms=T0)
        self.assertEqual(medidas['resolution_failures'], 1)
        self.assertEqual(medidas['economics_failures'], 1)
        self.assertEqual(medidas['operational_failures'], 2)

    def test_uma_linha_pode_estar_nas_duas_trilhas(self):
        # Resolução inválida E economia ausente na MESMA linha: o `elif` antigo
        # apagaria a segunda; as duas contagens têm de aparecer.
        ann = copy.deepcopy(self.resolvida())
        ann['resolution'] = {**ann['resolution'], 'status': 'CLOSED_TP2',
                             'net_r': None}
        duplo = prospective.operational_measurements([ann], [], {}, now_ms=T0)
        self.assertEqual(duplo['economics_failures'], 1)
        ann['resolution'] = {**ann['resolution'], 'status': 'INVALID'}
        so_resolucao = prospective.operational_measurements([ann], [], {}, now_ms=T0)
        self.assertEqual(so_resolucao['resolution_failures'], 1)
        self.assertEqual(so_resolucao['economics_failures'], 0)

    def test_coorte_vazia_nao_fabrica_protecao(self):
        medida = prospective.protection_measure([])
        self.assertIsNone(medida['unresolved_protection_failures'])
        self.assertIsNone(medida['protection_coverage_pct'])
        self.assertEqual(medida['protection_gap_reason'],
                         'NO_PROTECTION_OBLIGATION_OBSERVED')
        self.assertEqual(medida['protection_applicable'], 0)

    def test_metrica_shadow_nunca_vira_sl_real_ramp_ou_aceite_operacional(self):
        medida = self.medir(self.resolvida())
        self.assertEqual(medida['protection_scope'], 'SHADOW_SIMULATED')
        self.assertIs(medida['protection_proves_real_sl'], False)
        self.assertIs(medida['protection_scope_accepted_for_promotion'], False)
        self.assertEqual(medida['protection_decision_required'],
                         prospective.PROTECTION_DECISION_REQUIRED)

    def test_decisao_de_escopo_pendente_mantem_no_go_e_nao_promove(self):
        ann = self.resolvida()
        resumo = prospective.summarize_prospective([ann],
            started_at_ms=self.context['started_at_ms'],
            now_ms=self.context['manifest']['split']['as_of_ms'],
            enabled_playbooks=['TREND_PULLBACK'])
        self.assertIn(prospective.PROTECTION_DECISION_REQUIRED,
                      resumo['evidence']['essential_gaps'])
        self.assertEqual(resumo['gate']['verdict'], 'NO_GO')
        self.assertIn('ESSENTIAL_GAP', resumo['gate']['reason_codes'])
        self.assertFalse(resumo['promotable'])
        self.assertEqual(resumo['measurements']['protection_scope'],
                         'SHADOW_SIMULATED')

    def test_servico_nao_ganhou_segundo_executor_nem_loop(self):
        fonte = inspect.getsource(prospective)
        for proibido in ('place_order(', 'set_leverage(', 'send_telegram(',
                         'asyncio.create_task(', 'await session.commit('):
            self.assertNotIn(proibido, fonte)


if __name__ == '__main__':
    unittest.main()
