"""
P03.1E — Integração PostgreSQL REAL (asyncpg + SQLAlchemy + models reais).

Executado pelo runner num cluster PostgreSQL 16 DESCARTÁVEL (socket unix, sem
rede). NÃO é SQL copiado: chama `record_incident`, `_SqlIncidentRepo` e
`risk_service` REAIS. Prova, por CONEXÕES DISTINTAS, os invariantes centrais:

  incidente não resolvido ⇒ risk_state.trading_paused = true
  pausa+incidente no MESMO commit; rollback não deixa incidente parcial
  JSON sempre objeto ou SQL NULL (nunca array/JSON-null); união de IDs preservada
  reabertura fica elegível imediatamente; release estruturado; auto-resume gated

Sai 0 só se TODOS os cenários passarem; senão levanta AssertionError (exit != 0).
"""
import os
import sys
import asyncio
import socket as _socket

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

# ── Hermeticidade: só AF_UNIX (o socket local do cluster descartável). Qualquer
# TCP (AF_INET/AF_INET6) ou DNS é BLOQUEADO e CONTADO — prova que o código real
# nunca toca um banco externo (ex.: Railway) durante a integração. ────────────
_TCP_ATTEMPTS: list = []
_AF_INET, _AF_INET6 = _socket.AF_INET, _socket.AF_INET6
_orig_getaddrinfo = _socket.getaddrinfo


def _blocked_getaddrinfo(*a, **k):
    _TCP_ATTEMPTS.append(("getaddrinfo", a[:1]))
    raise OSError("HERMETICIDADE: DNS/getaddrinfo bloqueado no teste PG (só AF_UNIX)")


class _UnixOnlySocket(_socket.socket):
    def connect(self, address):
        if self.family in (_AF_INET, _AF_INET6):
            _TCP_ATTEMPTS.append(("connect", self.family, address))
            raise OSError("HERMETICIDADE: TCP (AF_INET/AF_INET6) bloqueado — só AF_UNIX")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family in (_AF_INET, _AF_INET6):
            _TCP_ATTEMPTS.append(("connect_ex", self.family, address))
            raise OSError("HERMETICIDADE: TCP (AF_INET/AF_INET6) bloqueado — só AF_UNIX")
        return super().connect_ex(address)


_socket.getaddrinfo = _blocked_getaddrinfo
_socket.socket = _UnixOnlySocket

# DATABASE_URL é setado pelo runner (socket unix). Importa DEPOIS.
import db  # noqa: E402
from sqlalchemy import text  # noqa: E402
import services.execution_reconciliation_service as ers  # noqa: E402
import services.risk_service as rk  # noqa: E402
from models.execution_incident import ExecutionIncident  # noqa: E402


def ok(msg):
    print(f"  ✓ {msg}")


async def _reset():
    async with db.get_session() as s:
        await s.execute(text("DELETE FROM execution_incidents"))
        state = await rk._get_or_create_state(s)   # ORM aplica defaults (dd_pct=0 etc.)
        state.trading_paused = False
        state.pause_manual = False
        state.pause_reason = None
        state.paused_at = None
        await s.commit()


async def _paused():
    async with db.get_session() as s:
        r = (await s.execute(text("SELECT trading_paused, pause_reason FROM risk_state WHERE id=1"))).first()
        return (bool(r[0]), r[1]) if r else (False, None)


async def _open_count():
    async with db.get_session() as s:
        return (await s.execute(text("SELECT count(*) FROM execution_incidents WHERE resolved_at IS NULL"))).scalar()


async def _cond(key):
    async with db.get_session() as s:
        return (await s.execute(text("SELECT conditional_ids, payload, jsonb_typeof(conditional_ids::jsonb) "
                                     "FROM execution_incidents WHERE incident_key=:k"), {"k": key})).first()


async def scenario_transaction_and_invariant():
    """Pausa+incidente no MESMO commit; observado por 3ª conexão sob concorrência
    com release. Trigger BEFORE INSERT com pg_sleep segura a transação; release
    concorrente BLOQUEIA na mesma advisory lock e, após o commit, vê incidente → STILL_OPEN."""
    await _reset()
    # trigger que atrasa o INSERT do incidente (garante janela de concorrência)
    async with db.get_session() as s:
        await s.execute(text("""
            CREATE OR REPLACE FUNCTION _p03_delay() RETURNS trigger AS $$
            BEGIN PERFORM pg_sleep(0.6); RETURN NEW; END; $$ LANGUAGE plpgsql;"""))
        await s.execute(text("DROP TRIGGER IF EXISTS _p03_delay_trg ON execution_incidents"))
        await s.execute(text("CREATE TRIGGER _p03_delay_trg BEFORE INSERT ON execution_incidents "
                             "FOR EACH ROW EXECUTE FUNCTION _p03_delay()"))
        await s.commit()

    release_result = {}

    async def _do_record():
        return await ers.record_incident(kind=ers.Kind.ENTRY_ORDER_UNKNOWN,
                                         symbol="BTC/USDT:USDT", client_order_id="tx1")

    async def _do_release():
        await asyncio.sleep(0.15)   # começa DURANTE o insert atrasado
        release_result["r"] = await rk.release_p03_pause(rk._P03_PAUSE_MARKER)

    rec, _ = await asyncio.gather(_do_record(), _do_release())

    # drop trigger
    async with db.get_session() as s:
        await s.execute(text("DROP TRIGGER IF EXISTS _p03_delay_trg ON execution_incidents"))
        await s.commit()

    assert rec.get("persisted") is True, "incidente não persistiu"
    paused, reason = await _paused()
    opened = await _open_count()
    assert opened == 1, f"esperado 1 incidente aberto, got {opened}"
    assert paused is True, "INVARIANTE VIOLADO: incidente aberto sem trading_paused"
    assert (reason or "").startswith(rk._P03_PAUSE_MARKER), f"pausa não é P03: {reason}"
    assert release_result["r"] != rk.RELEASE_RELEASED, f"release liberou indevidamente: {release_result['r']}"
    ok(f"pausa+incidente no mesmo commit; release concorrente={release_result['r']} (≠ RELEASED); "
       f"incidente aberto ⇒ paused=true")


async def scenario_release_first_then_record():
    await _reset()
    r0 = await rk.release_p03_pause(rk._P03_PAUSE_MARKER)   # sem pausa/incidente
    assert r0 in (rk.RELEASE_RELEASED,), f"release inicial inesperado: {r0}"
    rec = await ers.record_incident(kind=ers.Kind.ENTRY_ORDER_UNKNOWN, symbol="ETH/USDT:USDT", client_order_id="rf1")
    assert rec["persisted"] and (await _paused())[0] is True and (await _open_count()) == 1
    ok("release-antes / record-depois: incidente aberto ⇒ paused=true")


async def scenario_two_concurrent_records():
    await _reset()
    res = await asyncio.gather(
        ers.record_incident(kind=ers.Kind.CLEANUP_PENDING, symbol="SOL/USDT:USDT", client_order_id="cc"),
        ers.record_incident(kind=ers.Kind.CLEANUP_PENDING, symbol="SOL/USDT:USDT", client_order_id="cc"),
    )
    assert all(r["persisted"] for r in res), "algum record concorrente não persistiu"
    assert (await _open_count()) == 1, "records concorrentes duplicaram a linha"
    assert (await _paused())[0] is True
    ok("dois records concorrentes → 1 linha, paused=true")


async def scenario_record_vs_manual_resume():
    await _reset()
    await ers.record_incident(kind=ers.Kind.UNTRACKED_POSITION, symbol="BTC/USDT:USDT")
    res = await rk.set_manual_pause(False)   # operador tenta resumir com incidente aberto
    assert res["trading_paused"] is True, "resume manual liberou com incidente aberto"
    assert (await _paused())[0] is True and (await _open_count()) >= 1
    ok("record vs resume manual: pausa mantida (recarimbada P03)")


async def scenario_json_tristate_and_union():
    await _reset()
    repo = ers._SqlIncidentRepo()
    # objeto + None + JSON-null literal, e união S1→S2
    await repo.upsert("j", {"kind": ers.Kind.CLEANUP_PENDING, "symbol": "B",
                            "conditional_ids": {"sl": "S1"}, "min_known_fill": 0.5})
    await repo.upsert("j", {"kind": ers.Kind.CLEANUP_PENDING, "symbol": "B",
                            "conditional_ids": None, "min_known_fill": 0.2})     # None → omitido
    await repo.upsert("j", {"kind": ers.Kind.CLEANUP_PENDING, "symbol": "B",
                            "conditional_ids": {"sl": "S2"}})                    # S1→S2
    async with db.get_session() as s:  # injeta JSON-null literal legado e faz merge
        await s.execute(text("UPDATE execution_incidents SET payload='null'::json WHERE incident_key='j'"))
        await s.commit()
    await repo.upsert("j", {"kind": ers.Kind.CLEANUP_PENDING, "symbol": "B", "payload": {"a": 1}})
    cid, payload, typ = await _cond("j")
    assert typ == "object", f"conditional_ids não é objeto: {typ}"
    assert cid.get("sl") == "S2", f"perna sl errada: {cid}"
    assert set(["S1", "S2"]).issubset(set(cid.get("all") or [])), f"união all perdeu id: {cid}"
    row = await repo.get("j")
    assert row["min_known_fill"] == 0.5, "GREATEST regrediu"
    assert isinstance(payload, dict) and payload.get("a") == 1, f"payload não-objeto após json-null: {payload}"
    # dois None seguidos não quebram
    await repo.upsert("j", {"kind": ers.Kind.CLEANUP_PENDING, "symbol": "B", "conditional_ids": None, "payload": None})
    ok("JSON tri-state: objeto/None/json-null → objeto|NULL; união S1+S2; GREATEST monotônico")


async def scenario_reopen_elegible():
    await _reset()
    repo = ers._SqlIncidentRepo()
    await repo.upsert("re", {"kind": ers.Kind.ENTRY_ORDER_UNKNOWN, "symbol": "B", "client_order_id": "x"})
    async with db.get_session() as s:
        await s.execute(text("UPDATE execution_incidents SET resolved_at=now(), state='FLAT', "
                             "next_retry_at=now()+interval '15 min', last_error='old error', "
                             "attempts=5, claimed_by='old', lease_expires_at=now()+interval '9 min', "
                             "manual_reason='m' WHERE incident_key='re'"))
        await s.commit()
    await repo.upsert("re", {"kind": ers.Kind.ENTRY_ORDER_UNKNOWN, "symbol": "B", "client_order_id": "x"})
    row = await repo.get("re")
    assert row["state"] == "OPEN" and row["resolved_at"] is None and row["attempts"] == 0, f"reopen incompleto: {row}"
    assert row["claimed_by"] is None and row["lease_expires_at"] is None and row["manual_reason"] is None
    assert row["last_error"] is None, "last_error antigo não limpo"
    assert row["next_retry_at"] is not None and row["next_retry_at"] <= ers._now(), "next_retry_at não elegível"
    ok("reabertura via repo real: elegível imediatamente (backoff/erro/claim limpos)")


async def scenario_p03_survives_rollover_autoresume():
    """invariante #8: a pausa P03 (owner) NUNCA sofre auto-resume — nem por virada de
    DIA, nem de SEMANA UTC, mesmo com DD saudável. Só o release estruturado (zero
    incidentes) pode soltá-la."""
    await _reset()
    await ers.record_incident(kind=ers.Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT", client_order_id="rr1")
    assert (await _paused())[0] is True
    # força a condição de rollover: dia/semana "antigos" (NULL≠hoje) + paused_at 2 dias atrás.
    async with db.get_session() as s:
        await s.execute(text("UPDATE risk_state SET current_day_utc=NULL, current_week_utc=NULL, "
                             "paused_at = now() - interval '2 days' WHERE id=1"))
        await s.commit()
    await rk.update_and_check()      # dia E semana viram, DD saudável → tentaria auto-resume
    paused, reason = await _paused()
    assert paused is True, "INVARIANTE #8 VIOLADO: auto-resume soltou a pausa P03"
    assert (reason or "").startswith(rk._P03_PAUSE_MARKER), f"pausa P03 perdida no rollover: {reason}"
    assert (await _open_count()) == 1, "incidente P03 deveria seguir aberto"
    ok("pausa P03 sobrevive à virada de dia+semana (sem auto-resume; owner intacto)")


async def _set_existing_autopause(reason="DD diário -5.00% atingiu limite -5%", days_ago=2):
    """Simula uma pausa AUTOMÁTICA de DD pré-existente (não-P03, não-manual)."""
    async with db.get_session() as s:
        await rk._get_or_create_state(s)   # garante a linha id=1
        await s.execute(text("UPDATE risk_state SET trading_paused=true, pause_manual=false, "
                             "pause_reason=:r, paused_at=now() - make_interval(days => :d) WHERE id=1"),
                        {"r": reason, "d": days_ago})
        await s.commit()


async def scenario_existing_autopause_incident_rollover():
    """PRODUÇÃO reproduzida: pausa automática de DD pré-existente + incidente P03
    aberto (ensure_p03 PRESERVA a pausa DD, não carimba P03) + rollover dia/semana com
    DD saudável. Em 5904b963 o auto-resume soltava a pausa DD deixando
    `open_incidents=1, trading_paused=false`. update_and_check deve FORÇAR paused com
    incidente aberto."""
    await _reset()
    await _set_existing_autopause()
    await ers.record_incident(kind=ers.Kind.ENTRY_ORDER_UNKNOWN, symbol="BTC/USDT:USDT", client_order_id="ex1")
    assert (await _open_count()) == 1
    async with db.get_session() as s:   # força rollover dia+semana
        await s.execute(text("UPDATE risk_state SET current_day_utc=NULL, current_week_utc=NULL WHERE id=1"))
        await s.commit()
    await rk.update_and_check()
    paused, reason = await _paused()
    assert (await _open_count()) == 1, "incidente deveria seguir aberto"
    assert paused is True, "PRODUÇÃO: open_incidents=1 com trading_paused=false (auto-resume soltou a pausa)"
    ok("pausa DD pré-existente + incidente + rollover ⇒ permanece pausado (open⇒paused)")


async def scenario_update_then_record_paused():
    """Concorrência (update primeiro / record depois): estado final open=1, paused=true."""
    await _reset()

    async def _upd():
        return await rk.update_and_check()

    async def _rec():
        await asyncio.sleep(0.05)
        return await ers.record_incident(kind=ers.Kind.ENTRY_ORDER_UNKNOWN,
                                         symbol="ETH/USDT:USDT", client_order_id="ur1")

    await asyncio.gather(_upd(), _rec())
    assert (await _open_count()) == 1 and (await _paused())[0] is True, "update||record deixou open sem pausa"
    ok("update||record (update primeiro) ⇒ open=1, paused=true")


async def scenario_record_then_update_paused():
    """Concorrência (record primeiro / update depois): estado final open=1, paused=true.
    update_and_check com incidente aberto NUNCA solta a pausa (conta na mesma txn)."""
    await _reset()

    async def _rec():
        return await ers.record_incident(kind=ers.Kind.ENTRY_ORDER_UNKNOWN,
                                         symbol="SOL/USDT:USDT", client_order_id="ru1")

    async def _upd():
        await asyncio.sleep(0.05)
        return await rk.update_and_check()

    await asyncio.gather(_rec(), _upd())
    assert (await _open_count()) == 1 and (await _paused())[0] is True, "record||update deixou open sem pausa"
    ok("record||update (record primeiro) ⇒ open=1, paused=true")


async def scenario_rollback_no_partial():
    await _reset()
    # força erro no meio da transação: kind inválido? Não — força via conditional_ids array (rejeitado no build)
    try:
        await ers.persist_incident_with_p03_pause("bad", {"kind": ers.Kind.ENTRY_ORDER_UNKNOWN, "symbol": "B",
                                                          "conditional_ids": ["array-invalido"]}, "erro")
    except Exception:
        pass
    # o build rejeita array → ValueError → transação não commita → nada parcial
    assert (await _open_count()) == 0, "rollback deixou incidente parcial"
    ok("rollback: erro no meio não deixa incidente parcial")


async def main():
    await db.init_db()
    for fn in (scenario_json_tristate_and_union, scenario_reopen_elegible,
               scenario_transaction_and_invariant, scenario_release_first_then_record,
               scenario_two_concurrent_records, scenario_record_vs_manual_resume,
               scenario_p03_survives_rollover_autoresume,
               scenario_existing_autopause_incident_rollover,
               scenario_update_then_record_paused, scenario_record_then_update_paused,
               scenario_rollback_no_partial):
        await fn()
    await db.close_db()
    if _TCP_ATTEMPTS:
        raise AssertionError(f"HERMETICIDADE VIOLADA: {len(_TCP_ATTEMPTS)} tentativa(s) TCP/DNS: {_TCP_ATTEMPTS[:5]}")
    ok("hermeticidade: nenhuma conexão TCP/DNS — só o socket unix local (AF_UNIX)")
    print("PG_INTEGRATION_OK")


if __name__ == "__main__":
    asyncio.run(main())
