"""Infraestrutura da suíte do Agente Consultório.

Garantias desta suíte, na ordem de importância:

1. **Nunca toca produção.** `DATA_DIR` aponta para um diretório temporário criado
   no momento em que este arquivo é importado — antes de `config` e `database`
   serem carregados, já que `database.DB_PATH` é calculado no import. Um valor
   de `DATA_DIR` que pareça produção aborta a coleta.
2. **Nunca gasta API paga.** `GROQ_API_KEY` e `ANTHROPIC_API_KEY` são zeradas e
   `agent._call_llm` é substituído por um dublê em *todos* os testes
   (autouse). Se algum caminho escapar do dublê, o original levanta
   `RuntimeError` por falta de chave — falha, não cobra.
3. **Nunca envia mensagem.** O consultório de teste usa `whatsapp_provider="mock"`.

## Limite de interpretação destes testes

O dublê do modelo verifica o **backend**: dada uma saída do LLM, o sistema reage
corretamente? Ele **não** mede com que frequência o modelo real produz aquela
saída. Nada aqui autoriza concluir taxa de acerto, de falha ou qualidade do
Llama 3.3 / Claude Haiku. Avaliar o modelo real exige um conjunto de conversas
de referência, chave própria e orçamento — fora desta etapa (ver E12/C7).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

# ── 1. Isolamento, ANTES de qualquer import do backend ────────────────────────

_dd = os.environ.get("DATA_DIR", "").strip()
if _dd:
    if _dd.rstrip("/") in ("/data", ".", "") or "/app" in _dd:
        raise RuntimeError(
            f"DATA_DIR={_dd!r} parece ser produção. A suíte recusa rodar assim."
        )
else:
    _dd = tempfile.mkdtemp(prefix="agente-testes-")
    os.environ["DATA_DIR"] = _dd

os.environ["GROQ_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["WHATSAPP_PROVIDER"] = "mock"
os.environ.setdefault("ADMIN_PASSWORD", "suite-de-testes")
os.environ.setdefault("MASTER_KEY", "master-de-testes")
os.environ.setdefault("INSTANCE_MONITOR_ENABLED", "0")
# Sem empurrão automático no webhook: cada teste decide quando o worker
# roda. Senão um webhook de um teste processaria eventos durante outro.
os.environ.setdefault("INBOX_NUDGE_ON_WEBHOOK", "0")
os.environ.setdefault("INBOX_WORKER_ENABLED", "0")
os.environ.setdefault("OUTBOX_WORKER_ENABLED", "0")
os.environ.setdefault("BASE_URL", "http://localhost:8010")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import agent                    # noqa: E402
import calendar_service as cal  # noqa: E402
import database as db           # noqa: E402

assert os.path.dirname(db.DB_PATH).rstrip("/") == _dd.rstrip("/"), (
    f"banco fora do diretório temporário: {db.DB_PATH}"
)

SLUG = "consultorio-teste"
SLUG_B = "consultorio-teste-b"      # segundo tenant, para isolamento
PHONE = "5511900000001"
PERIODOS = {"manha": (6, 12), "tarde": (12, 18), "noite": (18, 24)}


# ── 2. Dublê do modelo ────────────────────────────────────────────────────────

class ModeloSimulado:
    """Fila de respostas JSON que o LLM "devolveria". Registra o que foi pedido."""

    def __init__(self) -> None:
        self.respostas: list[str] = []
        self.chamadas: list[dict] = []

    def responder(self, **campos) -> None:
        """Enfileira uma resposta no contrato do agente (intent/action/data/texto)."""
        corpo = {
            "intent": campos.pop("intent", "schedule"),
            "action": campos.pop("action", "none"),
            "data": campos.pop("data", {}),
            "response_text": campos.pop("response_text", "ok"),
        }
        assert not campos, f"campos desconhecidos: {sorted(campos)}"
        self.respostas.append(json.dumps(corpo, ensure_ascii=False))

    def responder_cru(self, texto: str) -> None:
        self.respostas.append(texto)

    def __call__(self, system: str, messages: list[dict], max_tokens: int = 512,
                 force_json: bool = True) -> str:
        self.chamadas.append({"system": system, "messages": messages})
        if not self.respostas:
            raise AssertionError(
                "o backend chamou o modelo mais vezes do que o teste previu"
            )
        return self.respostas.pop(0)

    @property
    def ultimo_contexto(self) -> str:
        """O bloco [CONTEXTO] da última chamada — para inspecionar os horários."""
        if not self.chamadas:
            return ""
        return self.chamadas[-1]["messages"][-1]["content"]


@pytest.fixture
def modelo(monkeypatch) -> ModeloSimulado:
    m = ModeloSimulado()
    monkeypatch.setattr(agent, "_call_llm", m)
    return m


@pytest.fixture(autouse=True)
def _sem_api_paga(monkeypatch):
    """Rede de segurança: nenhum teste pode chamar o LLM de verdade."""
    def _proibido(*a, **k):
        raise AssertionError("chamada ao LLM real bloqueada pela suíte")
    monkeypatch.setattr(agent, "_get_groq_client", _proibido)
    monkeypatch.setattr(agent, "_get_anthropic_client", _proibido)


@pytest.fixture(autouse=True)
def _sem_calendario_externo(monkeypatch):
    """Google/CalDAV desligados: o consultório de teste não tem credencial, mas
    isso deixa explícito e evita qualquer tentativa de rede."""
    import caldav_service
    import google_calendar_service
    for mod in (google_calendar_service, caldav_service):
        monkeypatch.setattr(mod, "create_event", lambda *a, **k: None)
        monkeypatch.setattr(mod, "update_event", lambda *a, **k: False)


# ── 3. Consultório fictício ───────────────────────────────────────────────────

def _criar_tenant(slug: str, nome: str) -> dict:
    if not db.get_tenant(slug):
        db.create_tenant(slug=slug, name=nome,
                         psychologist_name="Dra. Vitoria Alencar",
                         working_hours_start=8, working_hours_end=19,
                         session_minutes=50)
    db.update_tenant(slug, whatsapp_provider="mock", working_days="0,1,2,3,4",
                     blocked_hours="12,13", blocked_dates="", status="active",
                     dashboard_token=f"token-{slug}")
    return db.get_tenant(slug)


@pytest.fixture(scope="session", autouse=True)
def _banco():
    db.init_db()
    yield


@pytest.fixture
def tenant(_banco) -> dict:
    t = _criar_tenant(SLUG, "Consultório Teste")
    _limpar(t["id"])
    return db.get_tenant(SLUG)


@pytest.fixture
def tenant_b(_banco) -> dict:
    t = _criar_tenant(SLUG_B, "Consultório Teste B")
    _limpar(t["id"])
    return db.get_tenant(SLUG_B)


def _limpar(tenant_id: int) -> None:
    with db.get_conn() as conn:
        for tabela in ("appointments", "conversations", "patients", "agent_paused",
                       "slot_offers", "offers", "inbound_events", "outbox"):
            try:
                conn.execute(f"DELETE FROM {tabela} WHERE tenant_id=?", (tenant_id,))
            except Exception:
                pass      # tabela de etapa posterior ainda não existe
        try:
            conn.execute(
                "DELETE FROM applied_effects WHERE event_id NOT IN "
                "(SELECT id FROM inbound_events)")
        except Exception:
            pass


@pytest.fixture
def relogio(monkeypatch):
    """Relógio fixo para os módulos que leem o tempo.

    `agent` e `calendar_service` leem o tempo num único ponto (`_now`), então
    fixá-lo é suficiente para tornar deterministas os cálculos de agenda. Os
    resultados esperados nos testes que usam esta fixture são escritos à mão —
    nunca derivados do mesmo interpretador que o código usa.

    Uso:
        def test_x(relogio):
            relogio(datetime(2026, 10, 21, 10, 0))   # quarta-feira
    """
    import agent
    import calendar_service as cal

    def _fixar(instante: datetime):
        monkeypatch.setattr(agent, "_now", lambda: instante)
        monkeypatch.setattr(cal, "_now", lambda: instante)
        return instante

    return _fixar


# ── 4. Auxiliares de agenda ───────────────────────────────────────────────────

def agora() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Sao_Paulo")).replace(tzinfo=None)


def proxima_ocorrencia(weekday: int, dias_min: int = 7, hora: int = 9) -> datetime:
    """Próxima data com esse dia da semana a pelo menos `dias_min` dias.

    `dias_min=7` garante que a data fica **muito além** dos primeiros horários
    do calendário (um dia de expediente rende ~11 horários; 24 horários cobrem
    pouco mais de dois dias), sem depender de que dia é hoje.
    """
    base = agora().replace(hour=hora, minute=0, second=0, microsecond=0)
    d = base + timedelta(days=dias_min)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


def dia_util_distante(dias_min: int = 9, hora: int = 9) -> datetime:
    """Primeiro dia de expediente (seg–sex) a pelo menos `dias_min` dias.

    Usado quando o teste precisa de uma data **muito além** dos primeiros
    horários do calendário, sem depender de qual dia da semana é hoje.
    """
    d = agora().replace(hour=hora, minute=0, second=0, microsecond=0) \
        + timedelta(days=dias_min)
    while d.weekday() > 4:
        d += timedelta(days=1)
    return d


def pedir(data: datetime, periodo: str = "") -> str:
    """Mensagem de paciente pedindo uma DATA EXPLÍCITA (dd/mm).

    Por que data explícita: o sistema interpreta o pedido com
    `agent._parse_request`, e "segunda-feira" significa a próxima segunda — que
    depende de que dia a suíte roda. Um teste que calcule o alvo por um caminho
    e o sistema por outro passa ou falha conforme o dia da semana. Com dd/mm os
    dois chegam exatamente à mesma data.
    """
    txt = f"quero agendar dia {data.strftime('%d/%m')}"
    return f"{txt} de {periodo}" if periodo else txt


def data_do_pedido(texto: str):
    """A data que o SISTEMA entende no texto — fonte única da verdade do teste."""
    import agent
    return agent._parse_request(texto, agora())["date"]


def encher_agenda(tenant: dict, dias: int) -> int:
    """Ocupa todos os horários livres dos próximos `dias` dias com pacientes
    fictícios — simula consultório em operação com a agenda cheia."""
    livres = cal.get_available_slots(tenant, days_ahead=dias, limit=1000)
    limite = (agora() + timedelta(days=dias)).date()
    n = 0
    for s in livres:
        if s.date() < limite:
            db.create_appointment(tenant["id"], f"Ocupado {n+1}",
                                  f"551190000{2000+n}", s)
            n += 1
    return n


def consultas(tenant: dict, phone: str) -> list[datetime]:
    return [datetime.fromisoformat(a["scheduled_at"])
            for a in db.get_appointments_by_phone(tenant["id"], phone)
            if not a.get("cancelled")]


def sid(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


# ── Ofertas de horários (E03) ─────────────────────────────────────────────────
# Uma oferta nasce em `draft`/`not_sent` e só vale para resolver "quero o 1"
# depois de o envio ser **aceito pelo provedor**. Os auxiliares abaixo deixam
# essa suposição explícita em cada teste.

def oferta_aceita_pelo_provedor(tenant: dict, phone: str, slots: list) -> str:
    """Oferta cujo envio o provedor aceitou — a melhor evidência disponível.

    O nome é deliberado: aceite do provedor **não** prova que o paciente viu a
    mensagem. É só o que permite tratá-la como a lista vigente.
    """
    oid = db.offer_create(tenant["id"], phone, slots)["offer_id"]
    db.offer_mark_sent(oid, "provider_accepted")
    return oid


def oferta_nao_enviada(tenant: dict, phone: str, slots: list) -> str:
    """Oferta montada e ainda não enviada: o paciente não pode tê-la visto."""
    return db.offer_create(tenant["id"], phone, slots)["offer_id"]


def confirmar_envio_da_ultima_oferta(tenant: dict, phone: str) -> str | None:
    """Simula o outbox aceitando o envio da oferta mais recente.

    Necessário porque uma oferta montada pelo agente nasce em `draft`: ela só
    passa a valer quando o provedor aceita a mensagem que a mostra.
    """
    ctx = db.offer_context(tenant["id"], phone)
    ids = ctx["pendentes"] + ctx["incertas"]
    if ctx["ativa"]:
        ids.append(ctx["ativa"]["offer_id"])
    if not ids:
        return None
    ultimo = max((db.offer_get(i) for i in ids), key=lambda o: o["seq"])
    db.offer_mark_sent(ultimo["offer_id"], "provider_accepted")
    return ultimo["offer_id"]


def passar_o_tempo_do_backoff() -> None:
    """Libera eventos e envios que estão esperando o próximo horário de
    tentativa, sem precisar dormir."""
    with db.get_conn() as conn:
        conn.execute("UPDATE inbound_events SET next_attempt_at=NULL "
                     "WHERE state='pending'")
        conn.execute("UPDATE outbox SET next_attempt_at=NULL WHERE state='pending'")


def oferta_vigente(tenant: dict, phone: str):
    """Horários da oferta vigente (aceita e não expirada), ou None."""
    ctx = db.offer_context(tenant["id"], phone)
    return (ctx["ativa"] or {}).get("slots") if ctx["ativa"] else None
