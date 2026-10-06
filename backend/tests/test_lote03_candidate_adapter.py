"""Provas locais do contrato CANDIDATE; não ativam infraestrutura em produção.

Scanner real/engine V3/calibração real; só OHLC externo e autoridade humana
sintética são bordas. Transporte end-to-end permanece PENDENTE neste checkpoint.
"""
import asyncio
import copy
import os
import socket
import unittest
from unittest.mock import patch

from services import live_candidate_adapter_service as adapter
from services import score_v3_service as s3, score_v3_calibration_service as c
from services import recommendation_service as scanner, entry_intent_service as intents
from tests.test_lote02_preselection_capture import sinal
from tests.test_lote02_calibration_final_boundaries import T, B, CUT, NOW

FEATURES = {'adx': 28.0, 'htf_alignment_ratio': 1.0, 'structure_quality': 1.0,
    'level_distance_atr': .2, 'trigger_body_ratio': .8,
    'trigger_follow_through_atr': 1.0, 'rr_tp2': 3.0,
    'entry_distance_atr': 0.0, 'volume_ratio': 2.0,
    'spread_pct': .01, 'funding_pct': 0.0}


def authority(event=c.EVENT_TP1):
    cfg = s3.ScoreConfig()
    scored = s3.score(FEATURES, playbook='TREND_PULLBACK', side='long', config=cfg)
    rows = [{'opportunity_key': f'train-{bin}-{i}', 'score': bin * 10 + 5.,
        'label': i < 24, 'event': event, 'decision_ts_ms': T+B,
        'label_available_ts_ms': T+11*B} for bin in range(10) for i in range(30)]
    fitted = c.fit_calibration(rows, event=event, population=cfg.population,
        model_fingerprint=scored['model_fingerprint'], score_config_hash='h'*64,
        horizon_bars=12, bar_ms=B, censoring=c.CENSORING_RULES[0], payoff_ref='M',
        source='REPLAY', dataset_hash='d'*64, cutoff_ms=CUT, generated_at_ms=CUT+B,
        valid_until_ms=T+10000*B, versions={'score': s3.SCORE_VERSION})
    assert fitted['ok'], fitted
    oos = [{'opportunity_key': f'oos-{i}', 'score': 95., 'label': i < 24,
        'event': event, 'decision_ts_ms': CUT+2*B,
        'label_available_ts_ms': CUT+12*B} for i in range(30)]
    checked = c.validate_out_of_sample(fitted['artifact'], oos, now_ms=NOW)
    assert checked['ok'], checked
    return {'ok': True, 'mode': 'CANDIDATE', 'authority_hash': 'a'*64,
        'generation': 1, 'experiment_id': 1, 'purpose': 'CANARY',
        'bundle': {'operational_semantics': dict(adapter.SEMANTICS),
            # Identidade estável do bundle: a autoridade real sempre a carrega.
            'bundle_hash': 'e' * 64,
            'manifest': {'comparison_scope': 'SELECTION_ONLY', 'candidate': {
                'score_config': cfg.as_dict(), 'score_config_hash': 'h'*64,
                'selection_rule': {'kind': 'SCORE_V3_MIN_SCORE',
                    'playbook': 'TREND_PULLBACK', 'min_score': 60.}}},
            'calibration_artifact': checked['artifact']},
        'approval': {'approval_id': 'b'*64, 'test_only': True, 'limits': {
            'symbols': ['BTCUSDT'], 'playbooks': ['TREND_PULLBACK'],
            'max_orders': 1, 'max_risk_pct': .5, 'expires_at_ms': NOW+100000}}}


def decision(auth=None, **changes):
    values = dict(features=FEATURES, symbol='BTCUSDT', side='long', timeframe='1h',
        entry=100., stop_loss=98., tp1=103., tp2=106., now_ms=NOW)
    values.update(changes)
    return adapter.candidate_decision(auth or authority(), **values)


class CandidateAdapter(unittest.TestCase):
    def setUp(self):
        self.guard = patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS forbidden'))
        self.guard.start(); self.addCleanup(self.guard.stop)
        self.guard2 = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        self.guard2.start(); self.addCleanup(self.guard2.stop)

    def test_legacy_off_never_loads_authority_or_calls_factory(self):
        from services import operational_governance_service as governance
        with patch.dict(os.environ, {adapter.SELECTOR_ENV: 'LEGACY'}), \
                patch.object(governance, 'load_view', side_effect=AssertionError('new work')):
            result = asyncio.run(adapter.load_operational_view(lambda: (_ for _ in ()).throw(AssertionError())))
        self.assertEqual(result, {'ok': True, 'mode': 'LEGACY'})

    def test_invalid_mode_is_not_legacy(self):
        with patch.dict(os.environ, {adapter.SELECTOR_ENV: 'typo'}):
            self.assertFalse(asyncio.run(adapter.load_operational_view(None))['ok'])

    def test_real_v3_and_supported_oos_bin_exact_probability(self):
        result = decision()
        self.assertTrue(result['ok'], result)
        ctx = result['context']
        self.assertEqual(ctx['selection']['prob_tp1'], .8)
        self.assertIsNone(ctx['selection']['prob_tp2'])
        self.assertEqual(ctx['selection']['tier_source'], 'LEGACY_UNCHANGED')
        self.assertEqual(adapter.freeze_context(ctx), ctx)

    def test_wrong_event_never_becomes_tp1(self):
        result = decision(authority(c.EVENT_TP2))
        self.assertTrue(result['ok'], result)
        self.assertIsNone(result['context']['selection']['prob_tp1'])
        self.assertEqual(result['context']['selection']['prob_tp2'], .8)

    def test_fingerprint_mismatch_is_unavailable_not_legacy(self):
        auth = authority()
        auth['bundle']['calibration_artifact']['model_fingerprint'] = 'different'
        self.assertFalse(decision(auth)['ok'])

    def test_unknown_features_no_neutral_score_or_selection(self):
        result = decision(features={})
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason_code'], 'CANDIDATE_SCORE_UNAVAILABLE')

    def test_wrong_scope_and_missing_runtime_contract_refused(self):
        for field in ('operational_semantics', 'comparison_scope'):
            auth = authority()
            if field == 'operational_semantics': auth['bundle'][field] = {}
            else: auth['bundle']['manifest'][field] = 'MANAGEMENT_ONLY'
            self.assertFalse(decision(auth)['ok'])

    def test_expiry_and_approved_population_are_hard_boundaries(self):
        auth = authority()
        self.assertFalse(decision(auth, symbol='OTHERUSDT')['ok'])
        self.assertFalse(decision(auth, now_ms=auth['approval']['limits']['expires_at_ms'])['ok'])

    def test_tampered_context_not_resealed(self):
        context = decision()['context']
        context['selection']['score'] -= 1
        with self.assertRaises(ValueError): adapter.freeze_context(context)

    def test_context_must_match_real_recommendation_geometry(self):
        context = decision()['context']
        rec = {'operational_selection': context, 'symbol': 'BTCUSDT', 'timeframe': '1h',
            'direction': 'long', 'entry': 100., 'stop_loss': 98., 'tp2': 106., 'signal': {'tp1': 103.}}
        self.assertEqual(adapter.context_for_rec(rec), context)
        rec['stop_loss'] = 99.
        with self.assertRaises(ValueError): adapter.context_for_rec(rec)

    def test_risk_cap_never_increases_size_or_accepts_unknown(self):
        ctx = decision()['context']
        args = dict(entry=100., stop=98., qty=2., equity=1000., leverage=5)
        self.assertTrue(adapter.canary_risk_verdict(ctx, **args)['ok'])
        self.assertFalse(adapter.canary_risk_verdict(ctx, **{**args, 'qty': 3})['ok'])
        self.assertFalse(adapter.canary_risk_verdict(ctx, **{**args, 'equity': None})['ok'])
        self.assertEqual(args['qty'], 2.)

    def test_frozen_intent_and_proposal_bind_same_context_legacy_hash_unchanged(self):
        ctx = decision()['context']
        snapshot = intents.decision_snapshot({'entry': 100., 'stop_loss': 98., 'qty': 2.,
            'operational_selection': ctx, 'candidate_equity_usd': 1000.})
        self.assertEqual(snapshot['operational_selection'], ctx)
        proposal = dict(account_ref='acct', exchange='binance', intent_key='intent', symbol='BTCUSDT',
            side='BUY', order_type='MARKET', dispatch_id='cw-test', qty=2., created_at_ms=NOW)
        legacy = intents.freeze_dispatch_proposal(**proposal)
        self.assertNotIn('operational_selection', legacy)
        self.assertTrue(intents.proposal_matches_stored(legacy)['ok'])
        candidate = intents.freeze_dispatch_proposal(**proposal, operational_selection=ctx)
        self.assertTrue(candidate['ok'])
        candidate['operational_selection']['selection']['min_score'] -= 1
        self.assertFalse(intents.proposal_matches_stored(candidate)['ok'])

    def test_legacy_payload_no_new_null_field(self):
        signal = sinal(timeframe='1h', conf=90)
        rec = scanner._build_recommendation(signal, 90, 'A')
        self.assertNotIn('operational_selection', rec.model_dump())

    def test_real_alltf_scanner_chooses_v3_not_legacy_score(self):
        auth = authority()
        signals = [sinal(timeframe='1h', conf=99), sinal(timeframe='4h', conf=70)]
        for i, sig in enumerate(signals):
            sig.indicators.pivot_low = 99.
            sig.indicators.volume_ratio = 2. if i else .8
            sig.current_price = 100.
            sig.data_freshness = {'candle': {'quality': 'FRESH', 'symbol': sig.symbol,
                'timeframe': sig.timeframe, 'open_time_ms': NOW-7200000,
                'close_time_ms': NOW-3600000, 'observed_at_ms': NOW}}
            object.__setattr__(sig, '_r13_research_inputs', {
                'version': 'R13_POINT_IN_TIME_FEATURES_V1', 'quality': 'FRESH',
                'open': 99. if i else 99.9, 'low': 99., 'high': 100., 'close': 100.,
                'trigger_reference': 98., 'trigger_reference_ms': NOW-8000000,
                'candle_close_ms': NOW-3600000, 'observed_at_ms': NOW})
        async def local(_svc, symbol, tf):
            return next(sig for sig in signals if sig.timeframe == tf)
        with patch.object(scanner, 'SCAN_TFS', ['1h', '4h']), \
                patch.object(scanner, 'HIGH_TF_PATTERNS_ENABLED', False), \
                patch.object(scanner, '_analyze_symbol_tf_server', side_effect=local), \
                patch.object(scanner.time, 'time', return_value=NOW/1000):
            result = asyncio.run(scanner._best_tf_for_symbol_server(None, 'BTCUSDT',
                candidate_authority=auth))
        self.assertIsNotNone(result)
        self.assertEqual(result[0].timeframe, '4h')
        self.assertLess(result[1], 99)  # tuple continua com score legado para tier/sizing
        self.assertEqual(getattr(result[0], '_r13_candidate_context')['selection']['tier_source'], 'LEGACY_UNCHANGED')
