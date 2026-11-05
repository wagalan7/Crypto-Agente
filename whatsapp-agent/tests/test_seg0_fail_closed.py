"""SEG-0: correção mínima de segurança, escopo E06/E07.

Três caminhos que precisam falhar fechados:

1. **Token vazio nas APIs legadas do painel.** Empresa criada por
   `POST /admin/tenants` nasce com `dashboard_token = ''`. Sem recusa explícita, um
   cabeçalho ausente vira `WHERE dashboard_token = ''` e autentica como ela.
2. **Webhook Stripe sem segredo.** O `stripe==10.5.0` não recusa segredo vazio: um
   evento assinado com HMAC de chave vazia passa em `construct_event`.
3. **`setup_token` vazio no onboarding e no checkout.** A mesma empresa nasce com
   `setup_token = ''`, e `/onboarding/info` devolve o link do painel e o
   `webhook_token`: por aí, até um token de painel emitido depois ficava exposto.

Tudo aqui usa dados fictícios. Nenhum teste alcança a rede: a API Stripe é substituída
por um dublê e qualquer tentativa de conexão levanta erro (`_sem_rede`).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import socket
import time

import pytest
import stripe
from fastapi.testclient import TestClient

import config
import database as db
import main
from conftest import _limpar, dia_util_distante

SLUG_SEM_TOKEN = "seg0-sem-token"
SLUG_PENDENTE = "seg0-pendente"
PACIENTE_FICTICIO = "Paciente Ficticio Seg0"
SEGREDO_FICTICIO = "whsec_ficticio_seg0"
SETUP_TOKEN_FICTICIO = "setup-ficticio-seg0"
FIM_DO_PERIODO = 1893456000  # 2030-01-01 UTC


@pytest.fixture(autouse=True)
def _sem_rede(monkeypatch):
    def _bloqueado(self, endereco, *a, **k):
        raise AssertionError(f"tentativa de rede em teste: {endereco!r}")

    monkeypatch.setattr(socket.socket, "connect", _bloqueado)


@pytest.fixture
def cliente():
    return TestClient(main.app)


def _criar_pela_rota(cliente: TestClient, slug: str) -> dict:
    """Mesmo caminho de produção: POST /admin/tenants → tenant_service → database."""
    r = cliente.post("/admin/tenants", json={"name": f"Empresa {slug}", "slug": slug},
                     headers={"X-Master-Key": config.MASTER_KEY})
    assert r.status_code == 201, r.text
    return db.get_tenant(slug)


def _apagar(tenant_id: int) -> None:
    _limpar(tenant_id)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))


@pytest.fixture
def sem_token(_banco, cliente):
    """Empresa ativa, sem token de painel."""
    t = _criar_pela_rota(cliente, SLUG_SEM_TOKEN)
    assert t["dashboard_token"] == ""   # pré-condição do defeito
    assert t["active"] == 1
    yield t
    _apagar(t["id"])


@pytest.fixture
def pendente(_banco, cliente):
    """Empresa aguardando pagamento: é o que um evento forjado tentaria ativar."""
    t = _criar_pela_rota(cliente, SLUG_PENDENTE)
    db.update_tenant(SLUG_PENDENTE, status="pending_payment", setup_token=SETUP_TOKEN_FICTICIO)
    yield db.get_tenant(SLUG_PENDENTE)
    _apagar(t["id"])


# ── 1. Token vazio ────────────────────────────────────────────────────────────

ROTAS_LEGADAS = [
    ("GET", "/dashboard/api/appointments"),
    ("GET", "/dashboard/api/export"),
    ("GET", "/dashboard/api/slots"),
    ("GET", "/dashboard/api/billing-portal"),
    ("GET", "/dashboard/api/google/status"),
    ("POST", "/dashboard/api/google/disconnect"),
]

CABECALHOS_SEM_CREDENCIAL = {
    "ausente": {},
    "vazio": {"X-Dashboard-Token": ""},
    "espacos": {"X-Dashboard-Token": "   "},
}


@pytest.mark.parametrize("metodo,rota", ROTAS_LEGADAS)
@pytest.mark.parametrize("caso", sorted(CABECALHOS_SEM_CREDENCIAL))
def test_empresa_sem_token_nao_autentica_sem_credencial(cliente, sem_token, metodo, rota, caso):
    r = cliente.request(metodo, rota, headers=CABECALHOS_SEM_CREDENCIAL[caso])
    assert r.status_code == 403, f"{metodo} {rota} ({caso}) → {r.status_code}: {r.text[:200]}"


@pytest.mark.parametrize("consulta", ["", "?token=", "?token=%20%20"])
def test_export_lgpd_recusa_token_vazio_na_query(cliente, sem_token, consulta):
    """A exportação aceita token também por ?token=."""
    r = cliente.get(f"/dashboard/api/export{consulta}")
    assert r.status_code == 403, f"{consulta!r} → {r.status_code}: {r.text[:200]}"


def test_token_vazio_nao_vaza_paciente_da_empresa_sem_token(cliente, sem_token):
    db.create_appointment(sem_token["id"], PACIENTE_FICTICIO, "5511977770000", dia_util_distante())
    r = cliente.get("/dashboard/api/appointments")
    assert r.status_code == 403
    assert PACIENTE_FICTICIO not in r.text


def test_token_vazio_nao_altera_a_empresa_sem_token(cliente, sem_token):
    db.update_tenant(SLUG_SEM_TOKEN, google_refresh_token="refresh-ficticio")
    r = cliente.post("/dashboard/api/google/disconnect")
    assert r.status_code == 403
    assert db.get_tenant(SLUG_SEM_TOKEN)["google_refresh_token"] == "refresh-ficticio"


def test_token_vazio_nao_dispara_confirmacoes(cliente, sem_token, monkeypatch):
    """Rota isenta de MASTER_KEY: o token de painel é a única barreira."""
    disparos = []

    async def _gravar():
        disparos.append(True)
        return []

    monkeypatch.setattr(main.scheduler, "run_confirmations_now", _gravar)
    r = cliente.post("/admin/confirmacoes/disparar")
    assert r.status_code == 403
    assert not disparos


@pytest.mark.parametrize("token", ["", " ", "   ", "\t", "\n"])
def test_resolvedor_recusa_token_em_branco(sem_token, token):
    assert db.get_tenant_by_token(token) is None


def test_token_so_com_espacos_gravado_no_banco_nao_autentica(cliente, sem_token):
    """Valor em branco gravado por engano também não é credencial."""
    db.update_tenant(SLUG_SEM_TOKEN, dashboard_token="   ")
    assert db.get_tenant_by_token("   ") is None
    r = cliente.get("/dashboard/api/appointments", headers={"X-Dashboard-Token": "   "})
    assert r.status_code == 403
    assert cliente.get("/controle/%20%20%20").status_code == 403


@pytest.mark.parametrize("metodo,rota", [
    ("GET", "/dashboard/api/appointments"),
    ("GET", "/dashboard/api/export"),
    ("GET", "/dashboard/api/slots"),
    ("GET", "/dashboard/api/google/status"),
    ("POST", "/dashboard/api/google/disconnect"),
])
def test_empresa_com_token_continua_200(cliente, tenant, metodo, rota):
    r = cliente.request(metodo, rota, headers={"X-Dashboard-Token": tenant["dashboard_token"]})
    assert r.status_code == 200, f"{metodo} {rota} → {r.status_code}: {r.text[:200]}"


def test_token_valido_alcanca_a_propria_empresa_e_nao_a_sem_token(cliente, tenant, sem_token):
    db.create_appointment(sem_token["id"], PACIENTE_FICTICIO, "5511977770000", dia_util_distante())
    assert db.get_tenant_by_token(tenant["dashboard_token"])["slug"] == tenant["slug"]
    r = cliente.get("/dashboard/api/appointments",
                    headers={"X-Dashboard-Token": tenant["dashboard_token"]})
    assert r.status_code == 200
    assert PACIENTE_FICTICIO not in r.text


# Rotas por slug: já comparam com `if not _dt or not compare_digest(...)`. Ficam
# aqui como conferência de que o caminho vazio continua fechado.

@pytest.mark.parametrize("base", ["/dashboard/{slug}", "/dashboard/stream/{slug}"])
@pytest.mark.parametrize("consulta", ["", "?token=", "?token=%20"])
def test_rotas_por_slug_recusam_empresa_sem_token(cliente, sem_token, base, consulta):
    r = cliente.get(base.format(slug=SLUG_SEM_TOKEN) + consulta)
    assert r.status_code == 403, f"{base}{consulta} → {r.status_code}"


@pytest.mark.parametrize("base", ["/dashboard/{slug}", "/dashboard/stream/{slug}"])
@pytest.mark.parametrize("consulta", ["", "?token=", "?token=errado"])
def test_rotas_por_slug_recusam_credencial_errada(cliente, tenant, base, consulta):
    r = cliente.get(base.format(slug=tenant["slug"]) + consulta)
    assert r.status_code == 403, f"{base}{consulta} → {r.status_code}"


def test_painel_por_slug_abre_com_token_valido(cliente, tenant):
    r = cliente.get(f"/dashboard/{tenant['slug']}?token={tenant['dashboard_token']}")
    assert r.status_code == 200


def test_stream_por_slug_abre_com_token_valido(cliente, tenant, monkeypatch):
    async def _um_evento(_tenant_id):
        yield "data: ok\n\n"     # finito: o subscribe real fica aberto indefinidamente

    monkeypatch.setattr(main.events, "subscribe", _um_evento)
    r = cliente.get(f"/dashboard/stream/{tenant['slug']}?token={tenant['dashboard_token']}")
    assert r.status_code == 200
    assert "data: ok" in r.text


# ── 2. Webhook Stripe ─────────────────────────────────────────────────────────

CAMPOS_DE_COBRANCA = ("status", "stripe_customer_id", "stripe_subscription_id", "plan_expires_at")


def _cobranca(slug: str) -> dict:
    t = db.get_tenant(slug)
    return {c: t.get(c) for c in CAMPOS_DE_COBRANCA}


def _assinar(payload: bytes, chave: str) -> str:
    ts = int(time.time())
    assinado = f"{ts}.{payload.decode()}".encode()
    return f"t={ts},v1={hmac.new(chave.encode(), assinado, hashlib.sha256).hexdigest()}"


def _evento_de_ativacao(referencia: str = SETUP_TOKEN_FICTICIO) -> bytes:
    return json.dumps({
        "id": "evt_ficticio",
        "object": "event",
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_ficticio",
            "object": "checkout.session",
            "client_reference_id": referencia,
            "subscription": "sub_ficticia",
            "customer": "cus_ficticio",
        }},
    }).encode()


@pytest.fixture
def api_stripe(monkeypatch):
    """Neutraliza a API Stripe e o e-mail de ativação. Devolve os ids consultados."""
    consultas: list[str] = []

    def _retrieve(sub_id, *a, **k):
        consultas.append(sub_id)
        return {"current_period_end": FIM_DO_PERIODO}

    monkeypatch.setattr(stripe.Subscription, "retrieve", _retrieve)
    monkeypatch.setattr(main.stripe_svc, "_send_activation_email", lambda *a, **k: None)
    return consultas


def _postar(cliente: TestClient, payload: bytes, assinatura: str | None):
    cabecalhos = {"Content-Type": "application/json"}
    if assinatura is not None:
        cabecalhos["Stripe-Signature"] = assinatura
    return cliente.post("/webhooks/stripe", content=payload, headers=cabecalhos)


@pytest.mark.parametrize("segredo", ["", "   "], ids=["vazio", "espacos"])
def test_stripe_sem_segredo_recusa_evento_forjado(cliente, pendente, api_stripe, monkeypatch, segredo):
    """O ataque: sem segredo configurado, quem assina com a mesma chave em branco
    ativaria qualquer empresa cujo setup_token conhecesse."""
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", segredo)
    antes = _cobranca(SLUG_PENDENTE)
    payload = _evento_de_ativacao()

    r = _postar(cliente, payload, _assinar(payload, segredo))

    assert r.status_code == 503, f"{r.status_code}: {r.text[:200]}"
    assert _cobranca(SLUG_PENDENTE) == antes
    assert antes["status"] == "pending_payment"
    assert not api_stripe


@pytest.mark.parametrize("caso", ["sem_assinatura", "outra_chave"])
def test_stripe_sem_segredo_nem_chega_a_processar(cliente, pendente, api_stripe, monkeypatch, caso):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "")
    chamadas = []
    monkeypatch.setattr(main.stripe_svc, "handle_webhook",
                        lambda *a, **k: chamadas.append(a) or {"received": True})
    payload = _evento_de_ativacao()
    assinatura = None if caso == "sem_assinatura" else _assinar(payload, SEGREDO_FICTICIO)

    r = _postar(cliente, payload, assinatura)

    assert r.status_code == 503, f"{r.status_code}: {r.text[:200]}"
    assert not chamadas


def test_stripe_assinatura_valida_continua_aceita(cliente, pendente, api_stripe, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", SEGREDO_FICTICIO)
    payload = _evento_de_ativacao()

    r = _postar(cliente, payload, _assinar(payload, SEGREDO_FICTICIO))

    assert r.status_code == 200, r.text
    assert r.json() == {"received": True}
    assert _cobranca(SLUG_PENDENTE) == {
        "status": "active",
        "stripe_customer_id": "cus_ficticio",
        "stripe_subscription_id": "sub_ficticia",
        "plan_expires_at": "2030-01-01",
    }
    assert api_stripe == ["sub_ficticia"]


@pytest.mark.parametrize("chave", ["", "whsec_outra_chave", None],
                         ids=["chave_vazia", "chave_errada", "sem_assinatura"])
def test_stripe_com_segredo_recusa_assinatura_invalida(cliente, pendente, api_stripe, monkeypatch, chave):
    """Com o segredo configurado, o forjamento por chave vazia já não passa."""
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", SEGREDO_FICTICIO)
    antes = _cobranca(SLUG_PENDENTE)
    payload = _evento_de_ativacao()
    assinatura = None if chave is None else _assinar(payload, chave)

    r = _postar(cliente, payload, assinatura)

    assert r.status_code == 400, f"{r.status_code}: {r.text[:200]}"
    assert _cobranca(SLUG_PENDENTE) == antes
    assert not api_stripe


# ── 3. setup_token vazio ──────────────────────────────────────────────────────

URL_STRIPE_FICTICIA = "https://exemplo.invalid/stripe"
URL_MP_FICTICIA = "https://exemplo.invalid/mp"
TOKEN_DE_PAINEL_FICTICIO = "painel-ficticio-seg0"


@pytest.fixture
def sem_setup_com_painel(cliente, sem_token):
    """Empresa sem setup_token que recebeu token de painel pela rota do operador."""
    r = cliente.post(f"/admin/tenants/{SLUG_SEM_TOKEN}/dashboard-token",
                     headers={"X-Master-Key": config.MASTER_KEY})
    assert r.status_code == 200, r.text
    t = db.get_tenant(SLUG_SEM_TOKEN)
    assert t["setup_token"] == ""   # pré-condição do defeito
    assert t["dashboard_token"]
    return t


@pytest.fixture
def checkouts(monkeypatch):
    """Neutraliza a criação de pagamento nos dois provedores e registra as chamadas."""
    chamadas: list[tuple[str, str]] = []

    def _stripe(t, plan="mensal"):
        chamadas.append(("stripe", t["slug"]))
        return URL_STRIPE_FICTICIA

    def _mp(t, plan="mensal"):
        chamadas.append(("mp", t["slug"]))
        return URL_MP_FICTICIA

    monkeypatch.setattr(main.stripe_svc, "create_checkout_session", _stripe)
    monkeypatch.setattr(main.mp_svc, "create_subscription", _mp)
    return chamadas


@pytest.mark.parametrize("token", ["", " ", "   ", "\t", "\n"])
def test_resolvedor_de_setup_recusa_token_em_branco(sem_token, token):
    assert db.get_tenant_by_setup_token(token) is None


def test_onboarding_info_nao_entrega_credenciais_com_setup_vazio(cliente, sem_setup_com_painel):
    """O pior caso: sem esta recusa, o token de painel recém-emitido sai por aqui."""
    t = sem_setup_com_painel
    r = cliente.get("/onboarding/info?setup_token=")
    assert r.status_code == 404, f"{r.status_code}: {r.text[:300]}"
    assert t["dashboard_token"] not in r.text
    assert t["webhook_token"] not in r.text


@pytest.mark.parametrize("consulta", ["", "?token=", "?token=%20%20"])
def test_pagina_de_pagamento_recusa_setup_em_branco(cliente, sem_token, consulta):
    r = cliente.get(f"/onboarding/pagamento{consulta}")
    assert r.status_code == 404, f"{consulta!r} → {r.status_code}"


@pytest.mark.parametrize("rota", ["/checkout/stripe", "/checkout/mercadopago"])
@pytest.mark.parametrize("formulario", [{}, {"setup_token": ""}, {"setup_token": "   "}],
                         ids=["ausente", "vazio", "espacos"])
def test_checkout_recusa_setup_em_branco(cliente, sem_token, checkouts, rota, formulario):
    r = cliente.post(rota, data=formulario, follow_redirects=False)
    assert r.status_code == 404, f"{rota} → {r.status_code}: {r.headers.get('location')}"
    assert not checkouts


def test_setup_so_com_espacos_gravado_no_banco_nao_autentica(cliente, sem_token):
    db.update_tenant(SLUG_SEM_TOKEN, setup_token="   ")
    assert db.get_tenant_by_setup_token("   ") is None
    assert cliente.get("/onboarding/info?setup_token=%20%20%20").status_code == 404


def test_stripe_evento_valido_com_referencia_em_branco_nao_ativa(cliente, sem_token, api_stripe, monkeypatch):
    """Assinatura correta não basta: a referência em branco não identifica empresa."""
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", SEGREDO_FICTICIO)
    db.update_tenant(SLUG_SEM_TOKEN, status="pending_payment", setup_token="   ")
    antes = _cobranca(SLUG_SEM_TOKEN)
    payload = _evento_de_ativacao(referencia="   ")

    r = _postar(cliente, payload, _assinar(payload, SEGREDO_FICTICIO))

    assert r.status_code == 200, r.text
    assert _cobranca(SLUG_SEM_TOKEN) == antes
    assert not api_stripe


def test_mercado_pago_com_referencia_em_branco_nao_ativa(sem_token, monkeypatch):
    """Segundo chamador do resolvedor fora do main.py. A API do MP é substituída."""
    class _Resposta:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "authorized", "external_reference": "   "}

    class _ClienteMP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return _Resposta()

    monkeypatch.setattr(main.mp_svc.httpx, "Client", _ClienteMP)
    monkeypatch.setattr(main.mp_svc, "_send_activation_email", lambda *a, **k: None)
    db.update_tenant(SLUG_SEM_TOKEN, status="pending_payment", setup_token="   ")

    assert main.mp_svc.handle_webhook({"action": "updated", "data": {"id": "sub_mp_ficticia"}}) == {"received": True}

    t = db.get_tenant(SLUG_SEM_TOKEN)
    assert t["status"] == "pending_payment"
    assert not t["mp_subscription_id"]


def test_setup_valido_continua_funcionando(cliente, pendente, checkouts):
    info = cliente.get(f"/onboarding/info?setup_token={SETUP_TOKEN_FICTICIO}")
    assert info.status_code == 200, info.text
    assert info.json()["slug"] == SLUG_PENDENTE

    assert cliente.get(f"/onboarding/pagamento?token={SETUP_TOKEN_FICTICIO}").status_code == 200

    for rota, destino in (("/checkout/stripe", URL_STRIPE_FICTICIA),
                          ("/checkout/mercadopago", URL_MP_FICTICIA)):
        r = cliente.post(rota, data={"setup_token": SETUP_TOKEN_FICTICIO}, follow_redirects=False)
        assert r.status_code == 303, f"{rota} → {r.status_code}"
        assert r.headers["location"] == destino
    assert checkouts == [("stripe", SLUG_PENDENTE), ("mp", SLUG_PENDENTE)]


def test_painel_de_empresa_suspensa_com_setup_leva_ao_pagamento(cliente, pendente):
    db.update_tenant(SLUG_PENDENTE, status="suspended", dashboard_token=TOKEN_DE_PAINEL_FICTICIO)
    r = cliente.get(f"/dashboard/{SLUG_PENDENTE}?token={TOKEN_DE_PAINEL_FICTICIO}", follow_redirects=False)
    assert r.status_code == 303
    assert cliente.get(r.headers["location"]).status_code == 200


def test_painel_de_empresa_suspensa_sem_setup_nao_abre_pagamento_sem_dono(cliente, sem_setup_com_painel):
    """Caso de canto registrado: sem setup_token, o redirecionamento termina em 404.
    Antes abria a página de pagamento, cujo checkout buscava a primeira empresa com
    setup_token vazio e gerava cobrança sem referência para ativar ninguém."""
    t = sem_setup_com_painel
    db.update_tenant(SLUG_SEM_TOKEN, status="suspended")
    r = cliente.get(f"/dashboard/{SLUG_SEM_TOKEN}?token={t['dashboard_token']}", follow_redirects=False)
    assert r.status_code == 303
    assert cliente.get(r.headers["location"]).status_code == 404
