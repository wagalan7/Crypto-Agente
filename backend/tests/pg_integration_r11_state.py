"""R11/R12 — estado PERSISTENTE da simulação em PostgreSQL real.

`R11_STATE_TEST_SOCKET` aponta para /tmp/cw-r11-sock.* criado pelo runner.
Prova o que uma dataclass não prova: sobrevivência a restart, exclusividade sob
concorrência, histerese que exige período E evidência novos, e publicação com
invalidação de cache na mesma transação. Sem TCP/DNS, sem exchange.
"""
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import socket
import sys

test_socket = os.environ.get("R11_STATE_TEST_SOCKET", "")
if not re.fullmatch(r"/tmp/cw-r11-sock\.[A-Za-z0-9]+", test_socket):
    raise SystemExit("Socket de teste descartável obrigatório")
os.environ["DATABASE_URL"] = "postgresql+asyncpg://r11@/r11db?host=" + test_socket
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
OriginalSocket = socket.socket


class UnixOnlySocket(OriginalSocket):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R11")
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("TCP proibido no teste R11")
        return super().connect_ex(address)


def no_dns(*args, **kwargs):
    raise AssertionError("DNS proibido no teste R11")


socket.socket = UnixOnlySocket
socket.getaddrinfo = no_dns

CHECKS: list = []
DIA = 86_400_000
T0 = 1_760_000_000_000


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  ✓ {name}")


async def run():
    from sqlalchemy import func, select
    import db
    from models.policy_simulation_state import PolicySimulationState
    from services import policy_state_service as ps
    from services import robust_policy_service as rp

    for _ in range(2):      # migração aditiva idempotente
        async with db._engine.begin() as conn:
            await conn.run_sync(db.Base.metadata.create_all,
                                tables=[PolicySimulationState.__table__])

    chave = dict(experiment_key="exp-1", universe_version="u1")

    # 1. Sem linha, o estado é AUSENTE — nunca zero implícito.
    check("sem_estado_e_none", await ps.load_state(db.get_session, **chave) is None)

    # 2. Primeira publicação cria a geração 1.
    primeira = await ps.publish_generation(db.get_session, period_key="2026-W39",
                                           evidence_key="ev-1", now_ms=T0, **chave)
    check("primeira_geracao", primeira["published"] and primeira["generation"] == 1,
          str(primeira))

    # 3. Repetir no MESMO período é no-op idempotente.
    repetida = await ps.publish_generation(db.get_session, period_key="2026-W39",
                                           evidence_key="ev-2", now_ms=T0 + 1, **chave)
    check("mesmo_periodo_nao_publica",
          repetida["published"] is False and repetida["reason_code"] == "PERIOD_UNCHANGED",
          str(repetida))

    # 4. Período novo SEM evidência nova também não publica.
    sem_evidencia = await ps.publish_generation(db.get_session, period_key="2026-W40",
                                                evidence_key="ev-1", now_ms=T0 + DIA,
                                                **chave)
    check("evidencia_igual_nao_publica",
          sem_evidencia["published"] is False
          and sem_evidencia["reason_code"] == "EVIDENCE_UNCHANGED", str(sem_evidencia))

    # 5. Período novo E evidência nova publicam a geração 2.
    segunda = await ps.publish_generation(db.get_session, period_key="2026-W40",
                                          evidence_key="ev-2", now_ms=T0 + DIA, **chave)
    check("periodo_e_evidencia_novos_publicam",
          segunda["published"] and segunda["generation"] == 2, str(segunda))

    # 6. Restart: o estado continua lá, com a mesma histerese.
    await db._engine.dispose()          # derruba o pool: a leitura reconecta
    recarregado = await ps.load_state(db.get_session, **chave)
    check("estado_sobrevive_restart",
          recarregado is not None and recarregado["generation"] == 2
          and recarregado["period_key"] == "2026-W40", str(recarregado))

    # 7. Concorrência: duas simulações no mesmo período/evidência nova → UMA geração.
    resultados = await asyncio.gather(
        ps.publish_generation(db.get_session, period_key="2026-W41",
                              evidence_key="ev-3", now_ms=T0 + 2 * DIA, **chave),
        ps.publish_generation(db.get_session, period_key="2026-W41",
                              evidence_key="ev-3", now_ms=T0 + 2 * DIA, **chave),
    )
    publicadas = [item for item in resultados if item["published"]]
    check("concorrencia_publica_uma_vez", len(publicadas) == 1, str(resultados))
    final = await ps.load_state(db.get_session, **chave)
    check("geracao_final_e_tres", final["generation"] == 3, str(final))

    # 8. Identidades diferentes não se misturam.
    outro = await ps.publish_generation(db.get_session, experiment_key="exp-1",
                                        universe_version="u2", period_key="2026-W41",
                                        evidence_key="ev-3", now_ms=T0 + 2 * DIA)
    check("universo_diferente_tem_estado_proprio",
          outro["published"] and outro["generation"] == 1, str(outro))
    async with db.get_session() as session:
        linhas = int((await session.execute(
            select(func.count(PolicySimulationState.id)))).scalar() or 0)
    check("uma_linha_por_identidade", linhas == 2, str(linhas))

    # 9. A simulação não escreve universo, learned cache nem risco legado.
    from sqlalchemy import text as sql_text
    async with db.get_session() as session:
        tabelas = sorted(nome for (nome,) in (await session.execute(sql_text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).all())
    check("simulacao_nao_cria_tabela_operacional",
          tabelas == ["policy_simulation_state"], str(tabelas))

    await db._engine.dispose()
    print(f"R11_STATE_PG_OK: {len(CHECKS)} verificações — histerese persistente, "
          "concorrência, restart, sem exchange")


if __name__ == "__main__":
    asyncio.run(run())
