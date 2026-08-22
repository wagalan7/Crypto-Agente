-- P03.1C — Teste de RUNTIME PostgreSQL (cluster descartável, socket unix, sem rede).
-- Valida o CONTRATO SQL real do reconciliador: ON CONFLICT DO UPDATE (união
-- histórica de conditional_ids, GREATEST do lower-bound, reabertura atômica que
-- limpa claim/lease/manual_reason), claim/lease/fencing e a pausa CAS por
-- advisory lock. Qualquer divergência → RAISE EXCEPTION → psql sai != 0.
\set ON_ERROR_STOP on
SET client_min_messages = warning;

CREATE TABLE execution_incidents (
    id                 SERIAL PRIMARY KEY,
    incident_key       VARCHAR(160) UNIQUE NOT NULL,
    exchange           VARCHAR(20) DEFAULT 'binance',
    symbol             VARCHAR(40),
    kind               VARCHAR(40),
    state              VARCHAR(24) DEFAULT 'OPEN',
    client_order_id    VARCHAR(64),
    entry_order_id     VARCHAR(64),
    conditional_prefix VARCHAR(64),
    conditional_ids    JSON,
    side               VARCHAR(8),
    planned_qty        DOUBLE PRECISION,
    min_known_fill     DOUBLE PRECISION,
    planned_stop       DOUBLE PRECISION,
    payload            JSON,
    attempts           INTEGER DEFAULT 0,
    clean_observations INTEGER DEFAULT 0,
    next_retry_at      TIMESTAMPTZ,
    last_error         VARCHAR(500),
    manual_reason      VARCHAR(500),
    claimed_by         VARCHAR(64),
    claimed_at         TIMESTAMPTZ,
    lease_expires_at   TIMESTAMPTZ,
    created_at         TIMESTAMPTZ DEFAULT now(),
    updated_at         TIMESTAMPTZ DEFAULT now(),
    resolved_at        TIMESTAMPTZ
);

CREATE TABLE risk_state (
    id             INTEGER PRIMARY KEY,
    trading_paused BOOLEAN DEFAULT false,
    pause_reason   VARCHAR(255),
    pause_manual   BOOLEAN DEFAULT false,
    paused_at      TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ DEFAULT now()
);

-- Expressão de merge de conditional_ids IDÊNTICA à do serviço (cond_merge).
-- Encapsulada numa função para reusar nos cenários.
CREATE FUNCTION cond_merge(cur jsonb, inc jsonb) RETURNS json AS $$
  SELECT ((COALESCE(cur,'{}'::jsonb) || COALESCE(inc,'{}'::jsonb))
    || jsonb_build_object('all', (SELECT COALESCE(jsonb_agg(DISTINCT v),'[]'::jsonb) FROM (
         SELECT jsonb_array_elements_text(COALESCE(cur->'all','[]'::jsonb)) AS v
         UNION SELECT jsonb_array_elements_text(COALESCE(inc->'all','[]'::jsonb))
         UNION SELECT cur->>'sl' UNION SELECT cur->>'tp1' UNION SELECT cur->>'tp2'
         UNION SELECT inc->>'sl' UNION SELECT inc->>'tp1' UNION SELECT inc->>'tp2'
       ) s WHERE v IS NOT NULL)))::json;
$$ LANGUAGE sql;

-- ── Cenário 1: upsert atômico (ON CONFLICT DO UPDATE) ────────────────────────
-- insere {sl:S1}, lower-bound 0.5, depois faz conflito com {sl:S2}, lower-bound 0.2.
INSERT INTO execution_incidents (incident_key, kind, symbol, min_known_fill, conditional_ids, state)
VALUES ('k1','CLEANUP_PENDING','B', 0.5, '{"sl":"S1"}', 'OPEN');

INSERT INTO execution_incidents (incident_key, kind, symbol, min_known_fill, conditional_ids, planned_stop)
VALUES ('k1','CLEANUP_PENDING','B', 0.2, '{"sl":"S2"}', 9.0)
ON CONFLICT (incident_key) DO UPDATE SET
  min_known_fill = GREATEST(COALESCE(execution_incidents.min_known_fill,0), COALESCE(EXCLUDED.min_known_fill,0)),
  planned_stop = COALESCE(execution_incidents.planned_stop, EXCLUDED.planned_stop),
  conditional_ids = cond_merge(execution_incidents.conditional_ids::jsonb, EXCLUDED.conditional_ids::jsonb),
  state = CASE WHEN execution_incidents.resolved_at IS NOT NULL THEN 'OPEN' ELSE execution_incidents.state END,
  resolved_at = CASE WHEN execution_incidents.resolved_at IS NOT NULL THEN NULL ELSE execution_incidents.resolved_at END,
  attempts = CASE WHEN execution_incidents.resolved_at IS NOT NULL THEN 0 ELSE execution_incidents.attempts END,
  claimed_by = CASE WHEN execution_incidents.resolved_at IS NOT NULL THEN NULL ELSE execution_incidents.claimed_by END,
  updated_at = now();

DO $$
DECLARE r execution_incidents%ROWTYPE;
BEGIN
  SELECT * INTO r FROM execution_incidents WHERE incident_key='k1';
  IF (SELECT count(*) FROM execution_incidents) <> 1 THEN RAISE EXCEPTION 'upsert duplicou linha'; END IF;
  IF r.min_known_fill <> 0.5 THEN RAISE EXCEPTION 'GREATEST falhou: %', r.min_known_fill; END IF;
  IF r.planned_stop <> 9.0 THEN RAISE EXCEPTION 'COALESCE planned_stop falhou'; END IF;
  IF r.conditional_ids->>'sl' <> 'S2' THEN RAISE EXCEPTION 'perna sl atual errada'; END IF;
  IF NOT ((r.conditional_ids::jsonb->'all') ? 'S1' AND (r.conditional_ids::jsonb->'all') ? 'S2')
     THEN RAISE EXCEPTION 'uniao all perdeu id: %', r.conditional_ids; END IF;
END $$;

-- ── Cenário 2: reabertura atômica limpa claim/lease/manual ──────────────────
UPDATE execution_incidents SET state='FLAT', resolved_at=now(),
  claimed_by='old', lease_expires_at=now()+interval '9 min', manual_reason='x', attempts=5
  WHERE incident_key='k1';

INSERT INTO execution_incidents (incident_key, kind, symbol) VALUES ('k1','CLEANUP_PENDING','B')
ON CONFLICT (incident_key) DO UPDATE SET
  state = CASE WHEN execution_incidents.resolved_at IS NOT NULL THEN 'OPEN' ELSE execution_incidents.state END,
  resolved_at = CASE WHEN execution_incidents.resolved_at IS NOT NULL THEN NULL ELSE execution_incidents.resolved_at END,
  attempts = CASE WHEN execution_incidents.resolved_at IS NOT NULL THEN 0 ELSE execution_incidents.attempts END,
  manual_reason = CASE WHEN execution_incidents.resolved_at IS NOT NULL THEN NULL ELSE execution_incidents.manual_reason END,
  claimed_by = CASE WHEN execution_incidents.resolved_at IS NOT NULL THEN NULL ELSE execution_incidents.claimed_by END,
  lease_expires_at = CASE WHEN execution_incidents.resolved_at IS NOT NULL THEN NULL ELSE execution_incidents.lease_expires_at END,
  updated_at = now();

DO $$
DECLARE r execution_incidents%ROWTYPE;
BEGIN
  SELECT * INTO r FROM execution_incidents WHERE incident_key='k1';
  IF r.state <> 'OPEN' OR r.resolved_at IS NOT NULL OR r.attempts <> 0
     OR r.claimed_by IS NOT NULL OR r.lease_expires_at IS NOT NULL OR r.manual_reason IS NOT NULL
     THEN RAISE EXCEPTION 'reabertura nao limpou tudo: state=% claim=% lease=% manual=%',
       r.state, r.claimed_by, r.lease_expires_at, r.manual_reason; END IF;
END $$;

-- ── Cenário 3: claim/lease/fencing (UPDATE ... WHERE) ────────────────────────
-- claim livre → A pega
UPDATE execution_incidents SET claimed_by='A', claimed_at=now(), lease_expires_at=now()+interval '2 min'
  WHERE incident_key='k1' AND resolved_at IS NULL AND (claimed_by IS NULL OR lease_expires_at < now());
DO $$ BEGIN IF (SELECT claimed_by FROM execution_incidents WHERE incident_key='k1') <> 'A'
  THEN RAISE EXCEPTION 'claim A falhou'; END IF; END $$;
-- B tenta com lease de A vivo → 0 linhas
DO $$
DECLARE n int;
BEGIN
  UPDATE execution_incidents SET claimed_by='B', lease_expires_at=now()+interval '2 min'
    WHERE incident_key='k1' AND resolved_at IS NULL AND (claimed_by IS NULL OR lease_expires_at < now());
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'B roubou claim de A'; END IF;
END $$;
-- renew de A com lease vivo → ok; renew com lease vencido → 0
UPDATE execution_incidents SET lease_expires_at=now()-interval '1 min' WHERE incident_key='k1'; -- vence
DO $$
DECLARE n int;
BEGIN
  UPDATE execution_incidents SET lease_expires_at=now()+interval '2 min'
    WHERE incident_key='k1' AND claimed_by='A' AND resolved_at IS NULL AND lease_expires_at > now();
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'renew ressuscitou lease vencido'; END IF;
END $$;

-- ── Cenário 4: pausa CAS com advisory lock ──────────────────────────────────
INSERT INTO risk_state (id, trading_paused, pause_reason, pause_manual)
VALUES (1, true, 'P03-QUARANTINE: teste', true);
-- release P03 (marcador casa) sob advisory lock → 1 linha
DO $$
DECLARE n int;
BEGIN
  PERFORM pg_advisory_xact_lock(917283);
  UPDATE risk_state SET trading_paused=false, pause_manual=false, pause_reason=NULL, paused_at=NULL, updated_at=now()
    WHERE id=1 AND trading_paused=true AND pause_reason LIKE 'P03-QUARANTINE:%';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 1 THEN RAISE EXCEPTION 'release P03 CAS nao aplicou'; END IF;
END $$;
-- pausa manual (não-P03) → release P03 NÃO toca (0 linhas)
UPDATE risk_state SET trading_paused=true, pause_reason='Pausa manual via kill switch', pause_manual=true WHERE id=1;
DO $$
DECLARE n int;
BEGIN
  UPDATE risk_state SET trading_paused=false, pause_reason=NULL
    WHERE id=1 AND trading_paused=true AND pause_reason LIKE 'P03-QUARANTINE:%';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'release P03 apagou pausa manual'; END IF;
  IF (SELECT trading_paused FROM risk_state WHERE id=1) <> true THEN RAISE EXCEPTION 'pausa manual removida'; END IF;
END $$;

SELECT 'PG_RUNTIME_OK' AS result;
