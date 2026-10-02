"""Observação do boot/batch — janela ORIGINAL e completude, em PostgreSQL real.

`MANUALBOOT_TEST_SOCKET` aponta para /tmp/cw-mboot-sock.* criado pelo runner.
Converte em teste permanente a reprodução da revisão de `001a6685`
(`referencias/probe_boot.py`): o caller REAL `_detect_untracked_positions` usa a
MESMA leitura do ciclo, e era ela que perdia a janela do GET e filtrava `NaN`
como zero antes de validar completude.

Reais: parser `get_positions`, normalização, validação temporal,
`revalidate_active`, SQL, locks, commit, ownership e a decisão do scan.
Sintéticos: HTTP (somente GET), conta opaca e relógio.

RED medido na baseline `001a6685` antes da correção:
- GET de 21 s (limite 20 s) ⇒ scan `FLAT`, ack ACTIVE rev3 → CLOSED rev4,
  `manual_validation_blocked=false`, `ownership_guard(entry)` allowed=True;
- `positionAmt="NaN"` ⇒ linha eliminada por `_finite(size) or 0`, mesma
  liberação indevida.
"""
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import socket
import sys
from types import SimpleNamespace
from unittest.mock import patch

test_socket = os.environ.get("MANUALBOOT_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-mboot-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
# Banco e credenciais sintéticas ANTES de importar o app.
os.environ["DATABASE_URL"] = ("postgresql+asyncpg://mboot@/mbootdb?host="
                              + test_socket)
for _nome in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BYBIT_API_KEY",
              "BYBIT_API_SECRET"):
    os.environ.pop(_nome, None)
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido na observação do boot")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido na observação do boot")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido na observação do boot")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CHECKS: list = []
ESCOPO = "b" * 64
ALFA = "BOOTA/USDT:USDT"
BETA = "BOOTB/USDT:USDT"


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  ✓ {name}")


def agora() -> datetime:
    return datetime.now(timezone.utc)


async def run():
    from sqlalchemy import select, text
    import db
    from models.account_margin_epoch import AccountMarginEpoch as Epoch
    from models.manual_position_ack import ManualPositionAcknowledgement as Ack
    from models.recommendation_snapshot import RecommendationSnapshot  # noqa: F401
    from models.risk_state import RiskState  # noqa: F401
    from services import binance_signed_service as bss
    from services import execution_reconciliation_service as ers
    from services import manual_position_service as mps
    from services import shadow_trade_service as sts

    await db.init_db()

    # ── Borda HTTP: só GET, com latência e corpo programáveis ────────────
    CENARIO = {"latencia_ms": 0, "linhas": []}
    CHAMADAS: list = []
    relogio = {"offset_ms": 0}
    now_ms_real = mps._now_ms

    def now_ms():
        return now_ms_real() + relogio["offset_ms"]

    async def request(method, url):
        if method != "GET":
            raise AssertionError("mutação HTTP proibida nesta reprodução")
        CHAMADAS.append(url.split("?")[0])
        if "/fapi/v2/positionRisk" in url:
            relogio["offset_ms"] += CENARIO["latencia_ms"]
            corpo = CENARIO["linhas"]
        else:
            corpo = []
        return SimpleNamespace(status_code=200, headers={},
                              json=lambda: corpo)

    patches = [
        patch.object(bss, "is_configured", return_value=True),
        patch.object(bss, "_build_signed_url",
                     side_effect=lambda path, params=None:
                     "https://sintetico.invalido" + path),
        patch.object(bss, "_get_client",
                     return_value=SimpleNamespace(request=request)),
        patch.object(bss, "_ban_until_ms", 0),
        patch.object(bss, "_throttle_until_ms", 0),
        patch.object(mps, "current_account_scope", return_value=ESCOPO),
        patch.object(bss, "accounting_scope", lambda: ESCOPO),
        patch.object(mps, "_now_ms", side_effect=now_ms),
    ]
    for item in patches:
        item.start()

    def linha_raw(symbol=ALFA, *, amt="1", **extra):
        bruta = {"symbol": bss.to_binance(symbol), "positionAmt": amt,
                 "entryPrice": "100", "markPrice": "100",
                 "unRealizedProfit": "0", "leverage": "5", "notional": "100",
                 "positionSide": "LONG", "updateTime": 1_700_000_000_000}
        bruta.update(extra)
        return bruta

    async def epoca():
        async with db.get_session() as session:
            linha = (await session.execute(select(Epoch).where(
                Epoch.account_scope == ESCOPO))).scalar_one_or_none()
            return None if linha is None else {
                "manual": int(linha.manual_validation_generation or 0),
                "blocked": bool(linha.manual_validation_blocked)}

    async def semear(*, symbol=ALFA, state="ACTIVE", revision=3):
        """Fixture INDEPENDENTE: ack saudável, conta liberada, latch limpo."""
        mps.reset_local_validation_state()
        relogio["offset_ms"] = 0
        bss._positions_cache["data"] = None
        bss._positions_cache["ts"] = 0.0
        ers._boot_scan_safe = True
        ers._p03_latch_armed = False
        for dono in ("p03", "manual", "legacy"):
            sts.clear_execution_quarantine(owner=dono)
        async with db.get_session() as session:
            await session.execute(text(
                "DELETE FROM manual_position_acks WHERE account_scope=:s"),
                {"s": ESCOPO})
            await session.execute(text("DELETE FROM execution_incidents"))
            await session.execute(text(
                "UPDATE risk_state SET trading_paused=false, pause_manual=false,"
                " pause_reason=NULL, paused_at=NULL WHERE id=1"))
            await session.execute(text(
                "INSERT INTO account_margin_epochs (account_scope, exchange, "
                "market, generation, manual_validation_generation, "
                "manual_validation_blocked, updated_at) VALUES "
                "(:s,'binance','usdm_futures',0,0,false,now()) "
                "ON CONFLICT (account_scope, exchange, market) DO UPDATE SET "
                "manual_validation_blocked=false"), {"s": ESCOPO})
            impressao = mps.position_fingerprint(
                account_scope=ESCOPO, exchange="binance",
                market="usdm_futures", symbol=mps.canonical_symbol(symbol),
                side="buy", position_side="long",
                qty=mps.canonical_decimal("1"),
                entry_price=mps.canonical_decimal("100"),
                update_time_ms=1_700_000_000_000,
                contract_version="MANUAL_ACK_V1")
            linha = Ack(account_scope=ESCOPO, exchange="binance",
                        market="usdm_futures",
                        symbol=mps.canonical_symbol(symbol), quote="USDT",
                        side="buy", position_side="long", qty="1",
                        entry_price="100",
                        exchange_update_time_ms=1_700_000_000_000,
                        fingerprint=impressao,
                        contract_version="MANUAL_ACK_V1", state=state,
                        revision=revision, created_at=agora(),
                        updated_at=agora())
            session.add(linha)
            await session.commit()
            return int(linha.id)

    async def estado(ack_id):
        async with db.get_session() as session:
            linha = (await session.execute(select(Ack).where(
                Ack.id == ack_id))).scalar_one()
            return {"state": linha.state, "revision": int(linha.revision or 0),
                    "ended_reason": linha.ended_reason}

    async def precondicao_saudavel(ack_id, rotulo):
        """Nenhum caso começa com pausa/quarentena/causa residual."""
        epoca_antes = await epoca()
        async with db.get_session() as session:
            pausado = bool((await session.execute(select(
                RiskState.trading_paused).where(RiskState.id == 1))).scalar())
        estado_antes = await estado(ack_id)
        check(f"pre_{rotulo}_fixture_saudavel",
              epoca_antes["blocked"] is False and pausado is False
              and ers._boot_scan_safe is True
              and sts._EXECUTION_QUARANTINE_REASON is None
              and estado_antes["state"] == "ACTIVE"
              and mps.pending_validation_failure() is None,
              f"{epoca_antes} pausado={pausado} {estado_antes} "
              f"latch={sts._EXECUTION_QUARANTINE_REASON}")

    async def guarda_de_entrada(symbol=ALFA):
        return await mps.ownership_guard(symbol, action="entry",
                                        require_fresh_proof=True)

    async def caso_inseguro(rotulo, *, latencia_ms=0, linhas=(), symbol=ALFA):
        """Executa o caller REAL e devolve (scan, estado, guarda, época)."""
        ack_id = await semear(symbol=symbol)
        await precondicao_saudavel(ack_id, rotulo)
        CENARIO.update(latencia_ms=latencia_ms, linhas=list(linhas)
                       if linhas is not None else None)
        scan = await ers._detect_untracked_positions()
        return (scan, await estado(ack_id), await guarda_de_entrada(symbol),
                await epoca())

    # ══════════════════════════════════════════════════════════════════════
    #  A01 — GET de 21 s (contrato vigente: 20 s)
    # ══════════════════════════════════════════════════════════════════════
    # Controle do leitor direto: `observe_positions` SEMPRE recusou isso.
    await semear()
    CENARIO.update(latencia_ms=21_000, linhas=[])
    contexto_controle = await mps.capture_validation_context(
        scope=mps.SCOPE_ACCOUNT)
    observacao_controle = await mps.observe_positions()
    janela_controle = (observacao_controle["observed_end_ms"]
                       - observacao_controle["observed_start_ms"])
    veredito_controle = await mps.revalidate_active(
        observation=observacao_controle, context=contexto_controle)
    check("a01_controle_do_leitor_direto_recusa",
          janela_controle >= 21_000 and veredito_controle["ok"] is False
          and veredito_controle["reason_code"] in (
              mps.ACK_READ_WINDOW_TOO_LONG, mps.ACK_READ_TOO_OLD),
          f"janela={janela_controle} {veredito_controle}")

    scan, depois, guarda, ep = await caso_inseguro("a01", latencia_ms=21_000,
                                                   linhas=[])
    check("a01_boot_com_get_de_21s_nao_conclui_flat",
          scan["status"] == "UNKNOWN" and depois["state"] == "ACTIVE"
          and depois["revision"] == 3 and ers._boot_scan_safe is False
          and guarda["allowed"] is False and ep["blocked"] is True,
          f"{scan} {depois} boot_safe={ers._boot_scan_safe} {guarda} {ep}")

    # ══════════════════════════════════════════════════════════════════════
    #  A02 — positionAmt="NaN" pelo parser REAL
    # ══════════════════════════════════════════════════════════════════════
    await semear()
    CENARIO.update(latencia_ms=0, linhas=[linha_raw(amt="NaN")])
    observacao_nan = await mps.observe_positions()
    check("a02_controle_do_leitor_direto_recusa_nan",
          observacao_nan["ok"] is False and observacao_nan["complete"] is False
          and observacao_nan["reason_code"] == mps.ACK_POSITION_UNKNOWN,
          str(observacao_nan)[:200])

    scan, depois, guarda, ep = await caso_inseguro(
        "a02", linhas=[linha_raw(amt="NaN")])
    check("a02_nan_nao_vira_ausencia_de_posicao",
          scan["status"] == "UNKNOWN" and depois["state"] == "ACTIVE"
          and ers._boot_scan_safe is False and guarda["allowed"] is False
          and ep["blocked"] is True,
          f"{scan} {depois} boot_safe={ers._boot_scan_safe} {guarda} {ep}")

    # ══════════════════════════════════════════════════════════════════════
    #  A03 — None / inf / bool / coleção malformada / qty negativa
    # ══════════════════════════════════════════════════════════════════════
    # (a) Pelo parser REAL de `get_positions`: incompletude ⇒ UNKNOWN.
    casos_parser = [
        ("inf", [linha_raw(amt="Infinity")]),
        ("linha_malformada", ["texto"]),
        ("sem_update_time", [linha_raw(updateTime=0)]),
        ("sem_position_side", [linha_raw(positionSide="")]),
        ("sem_entrada", [linha_raw(entryPrice="x")]),
    ]
    for rotulo, linhas in casos_parser:
        scan, depois, guarda, ep = await caso_inseguro(
            f"a03_{rotulo}", linhas=linhas)
        contido = (ep["blocked"] is True
                   or sts._EXECUTION_QUARANTINE_REASON is not None)
        check(f"a03_{rotulo}_nunca_vira_flat",
              scan["status"] == "UNKNOWN" and depois["state"] == "ACTIVE"
              and ers._boot_scan_safe is False and guarda["allowed"] is False
              and contido,
              f"{rotulo}: {scan} {depois} {guarda} {ep} "
              f"latch={sts._EXECUTION_QUARANTINE_REASON}")

    # (b) `positionAmt` booleano: o parser REAL converte True→1.0, então a linha
    #     é uma posição LEGÍTIMA de 1 unidade. O invariante aqui é "nunca FLAT":
    #     o reconhecimento continua ACTIVE e o símbolo segue bloqueado.
    scan_bool, depois_bool, guarda_bool, _ = await caso_inseguro(
        "a03_bool", linhas=[linha_raw(amt=True)])
    check("a03_bool_virou_posicao_real_e_nao_flat",
          scan_bool["status"] != "FLAT" and depois_bool["state"] == "ACTIVE"
          and guarda_bool["allowed"] is False,
          f"{scan_bool} {depois_bool} {guarda_bool}")

    # (c) Contrato INTERNO da resposta (None/coleção malformada/qty negativa não
    #     chegam pelo parser real; aqui a BORDA devolve o payload malformado).
    get_positions_real = bss.get_positions
    for rotulo, payload in (("none", None), ("colecao_malformada", "nada"),
                            ("qty_negativa", [{**linha_raw(), "size": -1.0}])):
        ack_id = await semear()
        await precondicao_saudavel(ack_id, f"a03_{rotulo}")

        async def get_malformado(symbol=None, force=False, _p=payload):
            return {"ok": True, "positions": _p, "count": 0,
                    "exchange": "binance"}

        with patch.object(bss, "get_positions", get_malformado):
            scan = await ers._detect_untracked_positions()
        depois = await estado(ack_id)
        guarda = await guarda_de_entrada()
        ep = await epoca()
        contido = (ep["blocked"] is True
                   or sts._EXECUTION_QUARANTINE_REASON is not None)
        check(f"a03_{rotulo}_nunca_vira_flat",
              scan["status"] == "UNKNOWN" and depois["state"] == "ACTIVE"
              and ers._boot_scan_safe is False and guarda["allowed"] is False
              and contido,
              f"{rotulo}: {scan} {depois} {guarda} {ep} "
              f"latch={sts._EXECUTION_QUARANTINE_REASON}")
    assert bss.get_positions is get_positions_real

    # ══════════════════════════════════════════════════════════════════════
    #  A04 — [] válida + janela fresca ⇒ fechamento oficial correto
    # ══════════════════════════════════════════════════════════════════════
    ack_flat = await semear()
    await precondicao_saudavel(ack_flat, "a04")
    CENARIO.update(latencia_ms=0, linhas=[])
    scan_flat = await ers._detect_untracked_positions()
    estado_flat = await estado(ack_flat)
    epoca_flat = await epoca()
    check("a04_lista_vazia_valida_encerra_oficialmente",
          scan_flat["status"] == "FLAT" and estado_flat["state"] == "CLOSED"
          and estado_flat["revision"] == 4 and ers._boot_scan_safe is True
          and epoca_flat["blocked"] is False,
          f"{scan_flat} {estado_flat} {epoca_flat}")

    # Linha de quantidade ZERO finita e registrada continua observação VÁLIDA
    # (não é exposição e não é incompletude).
    ack_zero = await semear()
    await precondicao_saudavel(ack_zero, "a04_zero")
    CENARIO.update(latencia_ms=0, linhas=[
        {"symbol": bss.to_binance(BETA), "positionAmt": "0",
         "entryPrice": "0", "markPrice": "0", "unRealizedProfit": "0",
         "leverage": "5", "notional": "0", "positionSide": "BOTH",
         "updateTime": 1_700_000_000_000}])
    scan_zero = await ers._detect_untracked_positions()
    estado_zero = await estado(ack_zero)
    epoca_zero = await epoca()
    check("a04_zero_finito_legitimo_permanece_valido",
          scan_zero["status"] == "FLAT" and estado_zero["state"] == "CLOSED"
          and ers._boot_scan_safe is True and epoca_zero["blocked"] is False,
          f"{scan_zero} {estado_zero} {epoca_zero}")

    # ══════════════════════════════════════════════════════════════════════
    #  A05 — manual reconhecida + BOT em OUTRO símbolo
    # ══════════════════════════════════════════════════════════════════════
    ack_manual = await semear(symbol=ALFA)
    await precondicao_saudavel(ack_manual, "a05")
    CENARIO.update(latencia_ms=0, linhas=[linha_raw(ALFA, amt="1")])
    scan_a05 = await ers._detect_untracked_positions()
    estado_a05 = await estado(ack_manual)
    guarda_manual = await guarda_de_entrada(ALFA)
    guarda_bot = await guarda_de_entrada(BETA)
    protecao_bot = await mps.ownership_guard(
        BETA, action="place_protection_orders")
    check("a05_manual_preservada_e_bot_elegivel_em_outro_simbolo",
          estado_a05["state"] == "ACTIVE"
          and guarda_manual["allowed"] is False
          and guarda_manual["reason_code"] == mps.GUARD_MANUAL_SYMBOL
          and guarda_bot["allowed"] is True
          and protecao_bot["allowed"] is True
          and scan_a05["status"] in ("OK", "FLAT", "UNTRACKED"),
          f"{scan_a05} {estado_a05} manual={guarda_manual} bot={guarda_bot} "
          f"protecao={protecao_bot}")

    # ══════════════════════════════════════════════════════════════════════
    #  A06 — leitura fresca envelhece esperando ordens/lock
    # ══════════════════════════════════════════════════════════════════════
    ack_a06 = await semear()
    await precondicao_saudavel(ack_a06, "a06")
    CENARIO.update(latencia_ms=0, linhas=[])
    ordens_reais = mps.symbol_has_live_orders

    async def ordens_que_envelhecem(symbol):
        relogio["offset_ms"] += 21_000
        return await ordens_reais(symbol)

    with patch.object(mps, "symbol_has_live_orders", ordens_que_envelhecem):
        scan_a06 = await ers._detect_untracked_positions()
    estado_a06 = await estado(ack_a06)
    epoca_a06 = await epoca()
    guarda_a06 = await guarda_de_entrada()
    check("a06_envelhecer_esperando_ordens_mantem_recusa",
          scan_a06["status"] == "UNKNOWN" and estado_a06["state"] == "ACTIVE"
          and ers._boot_scan_safe is False and epoca_a06["blocked"] is True
          and guarda_a06["allowed"] is False,
          f"{scan_a06} {estado_a06} {epoca_a06} {guarda_a06}")

    # Idempotência: reexecutar o caso seguro não duplica ack nem incidente.
    ack_idem = await semear()
    CENARIO.update(latencia_ms=0, linhas=[])
    await ers._detect_untracked_positions()
    estado_um = await estado(ack_idem)
    await ers._detect_untracked_positions()
    estado_dois = await estado(ack_idem)
    async with db.get_session() as session:
        acks_totais = int((await session.execute(text(
            "SELECT count(*) FROM manual_position_acks WHERE account_scope=:s"),
            {"s": ESCOPO})).scalar() or 0)
        incidentes = int((await session.execute(text(
            "SELECT count(*) FROM execution_incidents"))).scalar() or 0)
    check("a_idempotente_reexecucao_nao_duplica",
          estado_um == estado_dois and acks_totais == 1 and incidentes == 0,
          f"{estado_um} {estado_dois} acks={acks_totais} inc={incidentes}")

    check("a_somente_get_http",
          all("/fapi/" in u for u in CHAMADAS) and len(CHAMADAS) > 0,
          f"{len(CHAMADAS)} chamadas")

    print(f"MANUAL_BOOT_PG_OK: {len(CHECKS)} verificações")
    for item in patches:
        item.stop()
    await db._engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
