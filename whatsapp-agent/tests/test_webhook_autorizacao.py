"""Webhooks, filtro de remetentes e autorização.

Exercita o comportamento atual pela API HTTP, com `_handle_message`
substituído por um gravador — nenhuma mensagem é processada pelo LLM nem enviada.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import database as db
import main
from conftest import SLUG, SLUG_B


@pytest.fixture
def cliente(tenant, tenant_b, monkeypatch):
    """TestClient com o processamento de mensagem interceptado."""
    recebidas: list[tuple] = []

    async def _gravar(tenant_, phone, text):
        recebidas.append((tenant_["slug"], phone, text))

    monkeypatch.setattr(main, "_handle_message", _gravar)
    c = TestClient(main.app)
    c.recebidas = recebidas
    return c


def _token(slug: str) -> str:
    return db.ensure_webhook_token(db.get_tenant(slug)["id"])


def _payload(texto="oi", jid="5511900000001@s.whatsapp.net", from_me=False,
             msg_id=None) -> dict:
    import uuid
    return {"data": {"key": {"id": msg_id or uuid.uuid4().hex,
                             "remoteJid": jid, "fromMe": from_me},
                     "message": {"conversation": texto}}}


# ── Autorização do webhook ────────────────────────────────────────────────────

def test_webhook_sem_token_e_rejeitado(cliente):
    _token(SLUG)
    r = cliente.post(f"/webhook/{SLUG}/evolution", json=_payload())
    assert r.status_code == 403
    assert not cliente.recebidas


def test_webhook_com_token_errado_e_rejeitado(cliente):
    _token(SLUG)
    r = cliente.post(f"/webhook/{SLUG}/evolution?token=errado", json=_payload())
    assert r.status_code == 403
    assert not cliente.recebidas


def test_webhook_com_token_correto_e_aceito(cliente, tenant):
    """Desde E03 o webhook **persiste** o evento e responde; quem processa é o
    worker. O que se verifica aqui é o registro durável, não a chamada direta."""
    t = _token(SLUG)
    r = cliente.post(f"/webhook/{SLUG}/evolution?token={t}",
                     json=_payload("quero remarcar"))
    assert r.status_code == 200 and r.json()["status"] == "queued"
    assert cliente.recebidas == [], "o webhook não deve processar na requisição"
    eventos = [e for e in db.inbound_list(states=("pending",))
               if e["tenant_id"] == tenant["id"]]
    assert [(e["phone"], e["body"]) for e in eventos] == \
        [("5511900000001", "quero remarcar")]


def test_webhook_aceita_token_por_header(cliente):
    t = _token(SLUG)
    r = cliente.post(f"/webhook/{SLUG}/evolution", json=_payload(),
                     headers={"X-Webhook-Token": t})
    assert r.status_code == 200


def test_token_de_um_consultorio_nao_vale_no_webhook_de_outro(cliente):
    _token(SLUG)
    tb = _token(SLUG_B)
    r = cliente.post(f"/webhook/{SLUG}/evolution?token={tb}", json=_payload())
    assert r.status_code == 403


def test_tenant_legado_sem_token_e_aceito_brecha_conhecida(cliente):
    """Comportamento ATUAL, deliberadamente retrocompatível: tenant sem
    `webhook_token` passa com warning. Brecha registrada em E01 §2.1; fechá-la é
    trabalho de E06. Este teste documenta a brecha — quando ela for fechada,
    falha e obriga a atualização.

    Nota: `update_tenant` **não** aceita `webhook_token` na lista de campos
    permitidos e ignora o parâmetro em silêncio, por isso o SQL direto."""
    t = db.get_tenant(SLUG)
    with db.get_conn() as conn:
        conn.execute("UPDATE tenants SET webhook_token='' WHERE id=?", (t["id"],))
    try:
        r = cliente.post(f"/webhook/{SLUG}/evolution", json=_payload())
        assert r.status_code == 200
    finally:
        with db.get_conn() as conn:
            conn.execute("UPDATE tenants SET webhook_token='' WHERE id=?", (t["id"],))


def test_update_tenant_ignora_campo_fora_da_lista_permitida():
    """Comportamento atual: campo não permitido é descartado sem erro. Registrado
    porque é fonte de "salvei e não mudou nada"."""
    t = db.get_tenant(SLUG)
    with db.get_conn() as conn:
        conn.execute("UPDATE tenants SET webhook_token='original' WHERE id=?", (t["id"],))
    db.update_tenant(SLUG, webhook_token="tentativa")
    assert db.get_tenant(SLUG)["webhook_token"] == "original"


# ── Filtro de remetentes que não são pacientes ────────────────────────────────

@pytest.mark.parametrize("jid", [
    "120363111111111111@g.us",          # grupo
    "status@broadcast",                  # status/stories
    "123456@newsletter",                 # canal
    "551194981102716191148@s.whatsapp.net",  # id sintético (>15 dígitos)
])
def test_remetente_nao_paciente_e_ignorado(cliente, jid):
    t = _token(SLUG)
    r = cliente.post(f"/webhook/{SLUG}/evolution?token={t}", json=_payload(jid=jid))
    assert r.json()["status"] == "ignored"
    assert not cliente.recebidas


def test_mensagem_do_proprio_numero_e_ignorada(cliente):
    t = _token(SLUG)
    r = cliente.post(f"/webhook/{SLUG}/evolution?token={t}",
                     json=_payload(from_me=True))
    assert r.json()["status"] == "ignored"
    assert not cliente.recebidas


def test_contato_com_telefone_oculto_lid_nao_e_descartado(cliente):
    """`@lid` é contato individual com telefone oculto — descartá-lo deixaria o
    paciente sem resposta."""
    t = _token(SLUG)
    r = cliente.post(f"/webhook/{SLUG}/evolution?token={t}",
                     json=_payload(jid="5511900000001@lid"))
    assert r.json()["status"] == "queued"


@pytest.mark.parametrize("tipo", ["audioMessage", "imageMessage", "documentMessage"])
def test_midia_na_evolution_e_descartada_limitacao_conhecida(cliente, tipo):
    """Comportamento ATUAL: o extractor da Evolution só lê texto, então áudio,
    imagem e documento são descartados em silêncio (E01 §2.1). Tratar isso é
    trabalho de E05. Documentado aqui para falhar quando mudar."""
    t = _token(SLUG)
    payload = {"data": {"key": {"remoteJid": "5511900000001@s.whatsapp.net",
                                "fromMe": False},
                        "message": {tipo: {"url": "https://exemplo.invalid/a"}}}}
    r = cliente.post(f"/webhook/{SLUG}/evolution?token={t}", json=payload)
    assert r.json()["status"] == "ignored"
    assert not cliente.recebidas


@pytest.mark.parametrize("raw,esperado", [
    ("5511900000001", False),
    ("5511900000001@s.whatsapp.net", False),
    ("5511900000001@lid", False),
    ("351968035444", False),
    ("120363111111111111@g.us", True),
    ("120363111111111111", True),
    ("status@broadcast", True),
    ("abc@newsletter", True),
    ("", True),
    ("5511949811027161911482", True),
])
def test_is_non_patient_id(raw, esperado):
    assert main._is_non_patient_id(raw) is esperado


# ── Autorização do painel e isolamento entre consultórios ─────────────────────

def test_token_de_painel_invalido_e_403(cliente):
    r = cliente.get("/dashboard/api/slots", headers={"X-Dashboard-Token": "nao-existe"})
    assert r.status_code == 403


def test_painel_resolve_o_consultorio_pelo_token_nao_por_parametro(cliente, tenant, tenant_b):
    """Isolamento: com o token de A não se alcança dado de B, e nenhum parâmetro
    de requisição permite escolher o tenant."""
    import calendar_service as cal
    from conftest import proxima_ocorrencia
    alvo = proxima_ocorrencia(0, dias_min=7, hora=16)
    db.create_appointment(tenant_b["id"], "Paciente de B", "5511988888888", alvo)

    r = cliente.get("/dashboard/api/appointments",
                    headers={"X-Dashboard-Token": tenant["dashboard_token"]})
    assert r.status_code == 200
    corpo = r.text
    assert "Paciente de B" not in corpo

    r2 = cliente.get(f"/dashboard/api/appointments?tenant_id={tenant_b['id']}",
                     headers={"X-Dashboard-Token": tenant["dashboard_token"]})
    assert "Paciente de B" not in r2.text


def test_stream_do_painel_exige_token_do_proprio_slug(cliente, tenant_b):
    r = cliente.get(f"/dashboard/stream/{SLUG}?token={tenant_b['dashboard_token']}")
    assert r.status_code == 403


# ── MASTER_KEY em /admin e /test ──────────────────────────────────────────────

def test_admin_sem_master_key_e_403(cliente):
    assert cliente.get(f"/admin/{SLUG}/appointments").status_code == 403


def test_admin_com_master_key_errada_e_403(cliente):
    assert cliente.get(f"/admin/{SLUG}/appointments?key=errada").status_code == 403


def test_admin_com_master_key_correta_passa(cliente):
    import config
    r = cliente.get(f"/admin/{SLUG}/appointments?key={config.MASTER_KEY}")
    assert r.status_code == 200


def test_rota_de_teste_tambem_exige_master_key(cliente):
    r = cliente.post(f"/test/{SLUG}/message",
                     json={"phone": "5511900000001", "text": "oi"})
    assert r.status_code == 403, "endpoint de simulação aberto chamaria o LLM real"


def test_rotas_de_saude_respondem(cliente):
    """Duas rotas de saúde existem: `/health` (sonda simples, aceita HEAD) e
    `/healthz` (detalhado, com `db` e `uptime_s`)."""
    assert cliente.get("/health").status_code == 200
    assert cliente.head("/health").status_code == 200
    r = cliente.get("/healthz")
    assert r.status_code == 200 and "db" in r.json()


def test_healthcheck_do_railway_aponta_para_rota_existente(cliente):
    """Guarda contra deriva de configuração: o `healthcheckPath` declarado em
    `railway.toml` precisa existir no app. Se alguém renomear a rota ou editar o
    arquivo, este teste falha antes de um deploy ficar sem healthcheck.

    O que este teste **não** prova: que a Railway esteja de fato aplicando este
    `railway.toml` (permanece NÃO VERIFICADO — exige o painel)."""
    import re
    from pathlib import Path
    toml = (Path(__file__).resolve().parents[1] / "railway.toml").read_text()
    m = re.search(r'healthcheckPath\s*=\s*"([^"]+)"', toml)
    assert m, "railway.toml sem healthcheckPath"
    caminho = m.group(1)
    registradas = {getattr(r, "path", "") for r in main.app.routes}
    assert caminho in registradas, (
        f"railway.toml aponta healthcheck para {caminho!r}, que não é rota do app"
    )
    assert cliente.get(caminho).status_code == 200
