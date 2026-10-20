"""Fronteiras prospectivas: scanner real, fontes locais e zero rede."""
import asyncio
import os
import socket
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import pandas as pd
from models.trade_signal import SignalDirection
from services import recommendation_service as rs, preselection_observation_service as pre
from services import decision_observation_service as obs, portfolio_service as portfolio
from services import regime_service as regime, news_filter_service as news
from services import snapshot_service as snaps, learning_service as learning
from services import score_v3_service as v3
from tests.test_lote02_preselection_capture import sinal, FonteFalsa, payload


def run_scan(signals, *, flags=None, positions=None, mode=pre.MODE_OBSERVE):
    symbols = sorted({symbol for symbol, _ in signals})
    async def analyze(_svc, symbol, tf): return signals.get((symbol, tf))
    async def macro():
        return dict(regime='NORMAL', block_all=False, block_alt_longs=False,
                    downgrade_alt_longs=False, downgrade_shorts=False, **(flags or {})) if not flags else {
                        'regime': 'NORMAL', 'block_all': False, 'block_alt_longs': False,
                        'downgrade_alt_longs': False, 'downgrade_shorts': False, **flags}
    async def no_blackout(): return {'active': False}
    async def no_cooldown(**kw): return set()
    async def no_learning(): return {}
    async def open_positions(): return positions or []
    before = set(obs._pending)
    pre.reset_coverage()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {pre.MODE_ENV: mode}))
        stack.enter_context(patch.object(rs, '_get_server_data_source', return_value=(FonteFalsa(symbols), 'local')))
        stack.enter_context(patch.object(rs, 'SCAN_TFS', ['1h', '4h']))
        stack.enter_context(patch.object(rs, 'HIGH_TF_PATTERNS_ENABLED', False))
        stack.enter_context(patch.object(rs, '_analyze_symbol_tf_server', side_effect=analyze))
        stack.enter_context(patch.object(news, 'get_blackout_status', side_effect=no_blackout))
        stack.enter_context(patch.object(regime, 'get_regime_status', side_effect=macro))
        stack.enter_context(patch.object(snaps, 'get_recently_stopped_symbols', side_effect=no_cooldown))
        stack.enter_context(patch.object(learning, 'compute_auto_adjustments', side_effect=no_learning))
        stack.enter_context(patch.object(portfolio, 'get_open_positions', side_effect=open_positions))
        stack.enter_context(patch.object(rs, 'PORTFOLIO_GUARD_ENABLED', positions is not None))
        recs = asyncio.run(rs.get_recommendations_via_vision(top_n=len(symbols), apply_guard=True))
    rows = [obs._pending[k] for k in set(obs._pending) - before]
    for k in set(obs._pending) - before: obs._pending.pop(k)
    return recs, rows, pre.coverage_snapshot()


class CaptureClosure(unittest.TestCase):
    def setUp(self):
        self.saved = dict(obs._pending)
        obs._pending.clear()
        self.addCleanup(lambda: (obs._pending.clear(), obs._pending.update(self.saved)))
        guard = patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS forbidden'))
        guard.start(); self.addCleanup(guard.stop)
        guard2 = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        guard2.start(); self.addCleanup(guard2.stop)

    def signals(self, count):
        return {(f'FAKE{i}USDT', tf): sinal(symbol=f'FAKE{i}USDT', timeframe=tf, conf=90 if tf == '1h' else 78)
                for i in range(count) for tf in ('1h', '4h')}

    def test_batch_52_admitted_and_champion_parity(self):
        signals = self.signals(26)
        on, rows, coverage = run_scan(signals)
        off, no_rows, _ = run_scan(signals, mode=pre.MODE_INACTIVE)
        self.assertEqual(len(rows), 52)
        self.assertEqual(coverage['candidates_observed'], 52)
        self.assertEqual(coverage['last_candidates_refused'], 0)
        self.assertEqual(no_rows, [])
        self.assertEqual([(r.symbol,r.timeframe,r.score) for r in on], [(r.symbol,r.timeframe,r.score) for r in off])

    def test_total_buffer_200_cap_honest_refusal(self):
        _, rows, coverage = run_scan(self.signals(105))
        self.assertEqual(len(rows), 200)
        self.assertEqual(coverage['candidates_observed'], 200)
        self.assertEqual(coverage['last_candidates_seen'], 210)
        self.assertEqual(coverage['last_candidates_refused'], 10)
        self.assertEqual(coverage['last_state'], pre.COVERAGE_INCOMPLETE)

    def test_portfolio_real_final_decision(self):
        signals = {('BTCUSDT','1h'): sinal(timeframe='1h', conf=90)}
        recs, rows, _ = run_scan(signals, positions=[])
        self.assertEqual(len(recs), 1)
        self.assertEqual(payload(rows[0])['outcome'], 'ACCEPTED')
        full = [{'symbol':f'P{i}USDT','direction':'long','risk_pct':0.1,'category':'other','phase':'pre_tp1'}
                for i in range(portfolio.MAX_OPEN_POSITIONS)]
        recs, rows, _ = run_scan(signals, positions=full)
        self.assertEqual(recs, [])
        p = payload(rows[0])
        self.assertEqual(p['outcome'], 'VETOED')
        self.assertEqual(p['observed_decision_scope'], 'FINAL_SCANNER_SELECTION')
        self.assertEqual(p['funnel']['first_blocker_reason'], 'PORTFOLIO_GUARD')

    def test_b_tier_downgrade_vetos_all_branches(self):
        for side, flag in ((SignalDirection.SHORT, 'downgrade_shorts'), (SignalDirection.LONG, 'downgrade_alt_longs')):
            with self.subTest(flag=flag):
                sig = sinal(symbol='ETHUSDT',timeframe='4h',conf=50,direction=side)
                recs, rows, _ = run_scan({('ETHUSDT','4h'):sig}, flags={flag:True})
                self.assertEqual(recs, [])
                self.assertEqual(len(rows), 1)
                self.assertEqual(payload(rows[0])['outcome'], 'VETOED')
        sig = sinal(symbol='ETHUSDT',timeframe='1h',conf=50,direction=SignalDirection.SHORT)
        sig.mtf={'higher_tfs':[{'timeframe':'4h','ema_aligned':'bullish'}]}
        with patch.object(rs,'_classify_tier_vision',return_value='B'), patch.object(regime,'CT_BRAKE_BLOCK',False):
            recs, rows, _ = run_scan({('ETHUSDT','1h'):sig})
        self.assertEqual(recs, [])
        self.assertEqual(payload(rows[0])['funnel']['first_blocker_reason'], 'COUNTER_TREND_DOWNGRADE_BELOW_TIER')

    def test_actual_trace_config_and_selection_score(self):
        sig = sinal(timeframe='1h', conf=90)
        with patch.object(rs,'SCORE_FORMULA_V2',False):
            _, rows, _ = run_scan({('BTCUSDT','1h'):sig})
        p=payload(rows[0])
        self.assertEqual(p['setup']['playbook_version'], 'LEGACY_V1')
        self.assertEqual(p['config']['formula_effective'], 'LEGACY_V1')
        self.assertEqual(p['config']['score'], sig.r08_score_trace['config'])
        self.assertEqual(p['features']['selection_score'], sig.r08_score_trace['stages']['selection_score']['value'])
        self.assertIsNotNone(rows[0]['score_trace'])

    def rich_signal(self, side='long'):
        now=int(time.time()*1000)
        sig=sinal(timeframe='1h',conf=90,direction=SignalDirection(side))
        sig.entry=100; sig.current_price=100; sig.stop_loss=98 if side=='long' else 102
        sig.tp1=103 if side=='long' else 97;sig.tp2=106 if side=='long' else 94
        sig.indicators.pivot_low=99;sig.indicators.pivot_high=101
        sig.data_freshness={'candle':{'quality':'FRESH','symbol':sig.symbol,'timeframe':sig.timeframe,
                                    'close_time_ms':now-1000,'open_time_ms':now-3601000,'observed_at_ms':now}}
        df=pd.DataFrame([dict(open=99,high=102,low=98,close=101)])
        rs._capture_research_inputs(sig,df)
        sig._r13_research_inputs.update(trigger_reference=100,trigger_reference_ms=now-7200000)
        sig.mtf={'alignment_score':-0.5,'higher_tfs':[
            {'timeframe':'4h','ema_aligned':'bullish' if side=='long' else 'bearish','data_freshness':{'candle':{**sig.data_freshness['candle'],'timeframe':'4h'}}},
            {'timeframe':'1d','ema_aligned':'mixed','data_freshness':{'candle':{**sig.data_freshness['candle'],'timeframe':'1d'}}}]}
        return sig

    def test_valid_producer_v3_path_no_future_or_signed_ratio(self):
        sig=self.rich_signal()
        f=rs._preselection_features(sig,80)
        self.assertEqual(f['htf_alignment_ratio'],0.5)
        self.assertEqual(f['structure_quality'],1)
        self.assertEqual(f['level_distance_atr'],0.5)
        self.assertEqual(f['trigger_body_ratio'],0.5)
        self.assertEqual(f['trigger_follow_through_atr'],0.5)
        self.assertEqual(v3.score({k:v for k,v in f.items() if isinstance(v,(int,float))},playbook='TREND_PULLBACK',side='long')['state'],v3.STATE_OK)
        before=dict(f)
        sig._r13_research_inputs['observed_at_ms']=int(time.time()*1000)+100000
        bad=rs._preselection_features(sig,80)
        self.assertIsNone(bad['trigger_body_ratio'])
        self.assertIsNone(bad['structure_quality'])
        self.assertEqual(before['structure_quality'],1)

    def test_reference_missing_or_future_does_not_become_open(self):
        sig=self.rich_signal()
        inputs=sig._r13_research_inputs
        inputs.pop('trigger_reference')
        self.assertIsNone(rs._preselection_features(sig,80)['trigger_follow_through_atr'])
        inputs['trigger_reference']=100;inputs['trigger_reference_ms']=int(time.time()*1000)
        self.assertIsNone(rs._preselection_features(sig,80)['trigger_follow_through_atr'])

    def test_off_never_constructs_features(self):
        with patch.object(rs,'_preselection_features',side_effect=AssertionError('off must not construct')), patch.object(rs,'_preselection_candidate',side_effect=AssertionError('off must not construct')):
            _,rows,cov=run_scan({('BTCUSDT','1h'):sinal(timeframe='1h',conf=90)},mode=pre.MODE_INACTIVE)
        self.assertEqual(rows,[]);self.assertEqual(cov['cycles'],0)

    def test_real_analysis_captures_local_closed_bar_privately(self):
        now=int(time.time()*1000);start=(now//3600000-100)*3600000
        df=pd.DataFrame([dict(timestamp=start+i*3600000,open=100+i*.1,
                            high=101+i*.1,low=99+i*.1,close=100.8+i*.1,volume=1000+i)
                         for i in range(100)])
        class Source:
            async def fetch_ohlcv(self,symbol,tf,limit):return df
        token=rs._PRE_CAPTURE.set(True)
        try:
            with patch.object(rs,'SCAN_OHLCV_CACHE_ENABLED',False):
                sig=asyncio.run(rs._analyze_symbol_tf_server(Source(),'TEST/USDT','1h'))
        finally:rs._PRE_CAPTURE.reset(token)
        self.assertIsNotNone(sig)
        self.assertEqual(sig._r13_research_inputs['close'],float(df.iloc[-1]['close']))
        self.assertNotIn('_r13_research_inputs',sig.model_dump())
        self.assertNotIn('research_inputs',sig.data_freshness)
        self.assertIsNone(sig._r13_research_inputs['trigger_reference'])

    def test_foreign_or_conflicting_htf_cannot_contribute(self):
        sig=self.rich_signal()
        first=sig.mtf['higher_tfs'][0]
        sig.mtf['higher_tfs']=[first,{**first,'ema_aligned':'bearish'},first]
        self.assertIsNone(rs._preselection_features(sig,80)['htf_alignment_ratio'])
        sig=self.rich_signal()
        sig.data_freshness['candle']['symbol']='OTHERUSDT'
        self.assertIsNone(rs._preselection_features(sig,80)['trigger_body_ratio'])

    def test_payload_versioned_budget_is_enforced(self):
        identity,_=pre.pre_selection_identity(symbol='BTCUSDT',timeframe='1h',side='long',
            trigger_candle_ms=1_780_000_000_000,playbook='CHAMPION_LEGACY',playbook_version='SCORE_V2')
        args=dict(identity=identity,outcome='ACCEPTED',decision_ts_ms=1_780_000_000_001,
                  setup={},funnel={},availability={},source={'unexpected':'x'*40000})
        with self.assertRaisesRegex(ValueError,'PAYLOAD_BUDGET_EXCEEDED'):
            pre.frozen_decision(**args,features={'adx':20})

    def test_provenance_without_features_is_v2_legacy_remains_v1(self):
        sig=sinal(timeframe='1h',conf=90)
        rs._compute_score(sig)
        row=rs._preselection_candidate(sig,75,stages=[rs._stage('CANDIDATE','PASSED')],accepted=True)
        with patch.object(pre,'collection_enabled',return_value=True):
            result=obs.observe_preselection([row])
        self.assertEqual((result['accepted'],result['skipped']),(1,0))
        self.assertEqual(payload(next(iter(obs._pending.values())))['schema_version'],pre.PRE_SCHEMA_VERSION_V2)
        identity,_=pre.pre_selection_identity(symbol='BTCUSDT',timeframe='1h',side='long',
            trigger_candle_ms=1_780_000_000_000,playbook='CHAMPION_LEGACY',playbook_version='SCORE_V2')
        legacy=pre.frozen_decision(identity=identity,outcome='ACCEPTED',decision_ts_ms=1_780_000_000_001,
                                  setup={},funnel={},availability={},source={})
        self.assertEqual(legacy['schema_version'],pre.PRE_SCHEMA_VERSION)

if __name__=='__main__': unittest.main()
