"""Regressões do hotfix: tokens fictícios, HTTP simulado, sem produção."""
from datetime import datetime, timedelta, timezone
import asyncio
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

import config
import database as db
import main
import evolution_provisioning as evo


def payload():
    return {'data': {'key': {'id': 'id-hotfix', 'remoteJid': '5511900000001@s.whatsapp.net'},
                     'message': {'conversation': 'oi'}}}


@pytest.fixture
def cliente(tenant, monkeypatch):
    monkeypatch.setattr(config, 'WEBHOOK_QUERY_TOKEN_UNTIL', '', raising=False)
    return TestClient(main.app)


def test_header_persiste_antes_do_ack(cliente, tenant):
    r = cliente.post(f"/webhook/{tenant['slug']}/evolution", json=payload(),
                     headers={'X-Webhook-Token': tenant['webhook_token']})
    assert r.status_code == 200 and r.json()['status'] == 'queued'
    assert any(e['tenant_id'] == tenant['id'] for e in db.inbound_list(states=('pending',)))


def test_query_sem_janela_explicita_rejeitada(cliente, tenant):
    r = cliente.post(f"/webhook/{tenant['slug']}/evolution", json=payload(),
                     params={'token': tenant['webhook_token']})
    assert r.status_code == 403


@pytest.mark.parametrize('offset', [-1, 72])
def test_query_vencida_ou_prazo_excessivo_rejeitada(cliente, tenant, monkeypatch, offset):
    deadline = (datetime.now(timezone.utc) + timedelta(hours=offset)).isoformat()
    monkeypatch.setattr(config, 'WEBHOOK_QUERY_TOKEN_UNTIL', deadline, raising=False)
    r = cliente.post(f"/webhook/{tenant['slug']}/evolution", json=payload(),
                     params={'token': tenant['webhook_token']})
    assert r.status_code == 403


def test_query_aceita_so_durante_transicao(cliente, tenant, monkeypatch):
    deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    monkeypatch.setattr(config, 'WEBHOOK_QUERY_TOKEN_UNTIL', deadline, raising=False)
    r = cliente.post(f"/webhook/{tenant['slug']}/evolution", json=payload(),
                     params={'token': tenant['webhook_token']})
    assert r.status_code == 200


def test_header_invalido_nao_e_salvo_por_query_correta(cliente, tenant, monkeypatch):
    deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    monkeypatch.setattr(config, 'WEBHOOK_QUERY_TOKEN_UNTIL', deadline, raising=False)
    r = cliente.post(f"/webhook/{tenant['slug']}/evolution", json=payload(),
                     params={'token': tenant['webhook_token']},
                     headers={'X-Webhook-Token': 'header-incorreto-ficticio'})
    assert r.status_code == 403


def test_credenciais_duplicadas_ou_contraditorias_rejeitadas(cliente, tenant):
    path = f"/webhook/{tenant['slug']}/evolution"
    for headers in [
        [('X-Webhook-Token', tenant['webhook_token']), ('X-Webhook-Token', 'errado')],
        {'X-Webhook-Token': tenant['webhook_token'], 'Client-Token': 'errado'},
    ]:
        assert cliente.post(path, json=payload(), headers=headers).status_code == 403


def test_tenant_sem_segredo_falha_fechado(cliente, tenant):
    with db.get_conn() as conn:
        conn.execute("UPDATE tenants SET webhook_token='' WHERE id=?", (tenant['id'],))
    try:
        assert cliente.post(f"/webhook/{tenant['slug']}/evolution", json=payload()).status_code == 403
    finally:
        db.ensure_webhook_token(tenant["id"])


def test_log_nao_revela_prefixo_de_token_rejeitado(cliente, tenant, caplog):
    secret = 'SENTINELA-WEBHOOK-NAO-LOGAR'
    with caplog.at_level(logging.WARNING):
        cliente.post(f"/webhook/{tenant['slug']}/evolution", json=payload(),
                     headers={'X-Webhook-Token': secret})
    assert 'SENTINEL' not in caplog.text


def test_create_set_e_find_conservam_header_sem_query(monkeypatch):
    calls = []
    real_client = httpx.AsyncClient
    token = 'segredo-ficticio-do-callback'
    url = 'https://app.test/webhook/clinica/evolution'

    def responder(req):
        import json
        calls.append(req)
        if req.url.path.endswith('/instance/create'):
            data = json.loads(req.content)
            assert data['webhook']['url'] == url
            assert data['webhook']['headers'] == {'X-Webhook-Token': token}
            return httpx.Response(201, json={'hash': 'ficticio'})
        if '/webhook/set/' in req.url.path:
            data = json.loads(req.content)['webhook']
            assert data['url'] == url and data['headers'] == {'X-Webhook-Token': token}
            return httpx.Response(201, json={})
        return httpx.Response(200, json={'url': url, 'headers': {'X-Webhook-Token': token},
                                         'enabled': True, 'events': ['MESSAGES_UPSERT']})

    monkeypatch.setattr(config, 'EVOLUTION_API_URL', 'https://evolution.test')
    monkeypatch.setattr(config, 'EVOLUTION_API_KEY', 'chave-global-ficticia')
    monkeypatch.setattr(evo.httpx, 'AsyncClient', lambda **kw: real_client(
        transport=httpx.MockTransport(responder), **kw))

    async def rodar():
        assert (await evo.create_instance('tenant-teste', url, webhook_token=token))['ok']
        assert (await evo.set_webhook('tenant-teste', url, webhook_token=token))['ok']
        for _ in range(2):
            info = await evo.get_webhook('tenant-teste', expected_token=token)
            assert info['auth_header_matches'] is True
            assert token not in repr(info)
    asyncio.run(rodar())
    assert len(calls) == 4


def test_set_nao_faz_fallback_sem_headers(monkeypatch):
    calls = []
    real_client = httpx.AsyncClient
    def responder(req):
        calls.append(req)
        return httpx.Response(400, text='corpo-com-segredo-ficticio')
    monkeypatch.setattr(config, 'EVOLUTION_API_URL', 'https://evolution.test')
    monkeypatch.setattr(evo.httpx, 'AsyncClient', lambda **kw: real_client(
        transport=httpx.MockTransport(responder), **kw))
    r = asyncio.run(evo.set_webhook('tenant-teste', 'https://app.test/webhook/t/evolution',
                                   webhook_token='segredo-ficticio'))
    assert not r['ok'] and len(calls) == 1
    assert 'corpo-com-segredo' not in repr(r)


def test_provisionamento_qr_usa_header_e_diagnostico_nao_expoe_segredo(cliente, tenant, monkeypatch):
    seen = []
    token = tenant['webhook_token']
    async def criar(instance, url, webhook_token=''):
        seen.append((url, webhook_token))
        return {'ok': True, 'instance_token': 'token-instancia-ficticio', 'qr': 'data:image/png;base64,AA=='}
    async def configurar(instance, url, webhook_token=''):
        seen.append((url, webhook_token))
        return {'ok': True, 'error': None}
    monkeypatch.setattr(config, 'EVOLUTION_API_URL', 'https://evolution.test')
    monkeypatch.setattr(config, 'EVOLUTION_API_KEY', 'ficticia')
    monkeypatch.setattr(evo, 'create_instance', criar)
    monkeypatch.setattr(evo, 'set_webhook', configurar)
    r = cliente.get('/dashboard/api/evolution/qr', headers={'X-Dashboard-Token': tenant['dashboard_token']})
    assert r.status_code == 200 and r.json()['ok']
    assert seen and all('?' not in url and value == token for url, value in seen)
    assert token not in r.text


def test_rotacao_em_duas_fases_sem_expor_nova_credencial_na_query(cliente, tenant, monkeypatch):
    from cryptography.fernet import Fernet
    import crypto
    monkeypatch.setattr(crypto, '_fernet', Fernet(Fernet.generate_key()))
    db.update_tenant(tenant['slug'], whatsapp_provider='evolution')
    old = tenant['webhook_token']
    path = f"/webhook/{tenant['slug']}/evolution"
    pending = db.stage_webhook_rotation(tenant['id'])
    assert db.evolution_callback_token(tenant['id']) == pending
    with db.get_conn() as conn:
        stored = conn.execute('SELECT pending_token FROM webhook_auth_rotation WHERE tenant_id=?',
                              (tenant['id'],)).fetchone()['pending_token']
    assert pending not in stored and stored.startswith('enc:v1:')
    for secret in (old, pending):
        assert cliente.post(path, json=payload(), headers={'X-Webhook-Token': secret}).status_code == 200
    monkeypatch.setattr(config, 'WEBHOOK_QUERY_TOKEN_UNTIL',
                        (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    assert cliente.post(path, json=payload(), params={'token': pending}).status_code == 403
    with pytest.raises(ValueError):
        db.stage_webhook_rotation(tenant['id'])
    # Finalizar só após verificação externa é decisão do operador, não do webhook.
    db.finalize_webhook_rotation(tenant['id'], pending)
    assert db.evolution_callback_token(tenant['id']) == pending
    assert cliente.post(path, json=payload(), headers={'X-Webhook-Token': old}).status_code == 403
    assert cliente.post(path, json=payload(), headers={'X-Webhook-Token': pending}).status_code == 200
    assert cliente.post(path, json=payload(), params={'token': pending}).status_code == 403
    assert 'pending_token' not in db.get_tenant(tenant['slug'])


def test_pendente_expirado_nao_autentica_nem_finaliza(cliente, tenant, monkeypatch):
    from cryptography.fernet import Fernet
    import crypto
    monkeypatch.setattr(crypto, '_fernet', Fernet(Fernet.generate_key()))
    db.update_tenant(tenant['slug'], whatsapp_provider='evolution')
    pending = db.stage_webhook_rotation(tenant['id'])
    with db.get_conn() as conn:
        conn.execute("UPDATE webhook_auth_rotation SET pending_until='2000-01-01T00:00:00+00:00' WHERE tenant_id=?",
                     (tenant['id'],))
    path = f"/webhook/{tenant['slug']}/evolution"
    assert cliente.post(path, json=payload(), headers={'X-Webhook-Token': pending}).status_code == 403
    with pytest.raises(ValueError):
        db.finalize_webhook_rotation(tenant['id'], pending)
    assert db.get_tenant(tenant['slug'])['webhook_token'] == tenant['webhook_token']


def test_rotacao_exige_cifra_e_nao_muda_tenant_sem_ela(cliente, tenant, monkeypatch):
    import crypto
    monkeypatch.setattr(crypto, '_fernet', None)
    db.update_tenant(tenant['slug'], whatsapp_provider='evolution')
    with pytest.raises(ValueError):
        db.stage_webhook_rotation(tenant['id'])
    assert db.webhook_rotation_state(tenant['id']) == {}


def test_filtros_locais_redigem_query_e_nao_alteram_path():
    from webhook_security import AccessQueryRedaction
    filtro = AccessQueryRedaction()
    record = logging.LogRecord('uvicorn.access', logging.INFO, '', 0,
                               '%s - "%s %s HTTP/%s" %d',
                               ('127.0.0.1', 'POST', '/webhook/teste/evolution?token=SEGREDO', '1.1', 200), None)
    filtro.filter(record)
    assert 'SEGREDO' not in record.getMessage() and '/webhook/teste/evolution HTTP' in record.getMessage()
    record = logging.LogRecord('httpx', logging.INFO, '', 0,
                               'HTTP Request: GET https://api.test/token/SEGREDO/x?token=OUTRO "HTTP/1.1 200"', (), None)
    filtro.filter(record)
    assert 'SEGREDO' not in record.getMessage() and 'OUTRO' not in record.getMessage()


def test_erro_e_find_legado_nao_expoem_payload_headers_ou_query(monkeypatch):
    real_client = httpx.AsyncClient
    token = 'TOKEN-FICTICIO-NAO-RETORNAR'
    def responder(req):
        return httpx.Response(200, json={'url': f'https://app.test/webhook/t/evolution?token={token}',
                                         'enabled': True, 'headers': {'X-Webhook-Token': token}})
    monkeypatch.setattr(config, 'EVOLUTION_API_URL', 'https://evolution.test')
    monkeypatch.setattr(evo.httpx, 'AsyncClient', lambda **kw: real_client(
        transport=httpx.MockTransport(responder), **kw))
    r = asyncio.run(evo.get_webhook('tenant-teste', expected_token=token))
    assert token not in repr(r) and r['has_query'] and r['auth_header_matches']


@pytest.mark.parametrize('headers', [
    {'X-Webhook-Token': 'SECRETO', 'Client-Token': 'OUTRO', 'content-type': 'application/json'},
    [('X-Webhook-Token', 'SECRETO'), ('content-type', 'application/json')],
])
def test_sentry_nao_recebe_header_query_ou_corpo_de_callback(headers):
    from webhook_security import redact_sentry_event
    event = {'request': {'url': 'https://app.test/webhook/t/evolution?token=SECRETO',
                          'query_string': 'token=SECRETO', 'headers': headers,
                          'data': {'texto': 'conteudo-ficticio-do-paciente'}}}
    r = redact_sentry_event(event, {})
    assert 'SECRETO' not in repr(r) and 'conteudo-ficticio' not in repr(r)
    assert 'application/json' in repr(r)


@pytest.mark.parametrize('deadline', ['', 'invalida', '2099-01-01T00:00:00', '2099-01-01T00:00:00+00:00'])
def test_deadline_falha_fechado(monkeypatch, deadline):
    from webhook_security import query_transition_open
    monkeypatch.setattr(config, 'WEBHOOK_QUERY_TOKEN_UNTIL', deadline)
    assert not query_transition_open(datetime(2026, 10, 20, tzinfo=timezone.utc))


def test_deadline_absoluto_nao_reabre_em_reinicio(monkeypatch):
    from webhook_security import query_transition_open
    monkeypatch.setattr(config, 'WEBHOOK_QUERY_TOKEN_UNTIL', '2026-10-21T12:00:00+00:00')
    assert query_transition_open(datetime(2026, 10, 21, 11, tzinfo=timezone.utc))
    assert not query_transition_open(datetime(2026, 10, 21, 12, tzinfo=timezone.utc))
    assert not query_transition_open(datetime(2026, 10, 22, 0, tzinfo=timezone.utc))


@pytest.mark.parametrize('url', [
    'https://app.test/webhook/t/evolution?token=ficticio',
    'https://user:password@app.test/webhook/t/evolution',
    'https://app.test/webhook/t/evolution#token',
])
def test_provisionamento_recusa_segredo_na_url_sem_http(monkeypatch, url):
    def proibido(**kw):
        pytest.fail('nao deve chamar rede com URL insegura')
    monkeypatch.setattr(evo.httpx, 'AsyncClient', proibido)
    with pytest.raises(ValueError):
        asyncio.run(evo.set_webhook('tenant-teste', url, webhook_token='ficticio'))


def test_token_outro_tenant_nao_autentica_header(cliente, tenant, tenant_b):
    r = cliente.post(f"/webhook/{tenant['slug']}/evolution", json=payload(),
                     headers={'X-Webhook-Token': tenant_b['webhook_token']})
    assert r.status_code == 403


@pytest.mark.parametrize('jid', ['120363000000@g.us', 'status@broadcast', 'canal@newsletter'])
def test_header_preserva_filtro_nao_paciente(cliente, tenant, jid):
    body = payload()
    body['data']['key']['remoteJid'] = jid
    r = cliente.post(f"/webhook/{tenant['slug']}/evolution", json=body,
                     headers={'X-Webhook-Token': tenant['webhook_token']})
    assert r.status_code == 200 and r.json()['status'] == 'ignored'


def test_migracao_idempotente_preserva_credencial_e_nao_rotaciona(tenant):
    old = tenant['webhook_token']
    db.init_db()
    db.init_db()
    assert db.get_tenant(tenant['slug'])['webhook_token'] == old
    assert db.webhook_rotation_state(tenant['id']) == {}


def test_chave_errada_nao_autentica_ciphertext_nem_reprovisiona(cliente, tenant, monkeypatch):
    from cryptography.fernet import Fernet
    import crypto
    monkeypatch.setattr(crypto, '_fernet', Fernet(Fernet.generate_key()))
    db.update_tenant(tenant['slug'], whatsapp_provider='evolution')
    token = db.stage_webhook_rotation(tenant['id'])
    with db.get_conn() as conn:
        stored = conn.execute('SELECT pending_token FROM webhook_auth_rotation WHERE tenant_id=?',
                              (tenant['id'],)).fetchone()['pending_token']
    monkeypatch.setattr(crypto, '_fernet', Fernet(Fernet.generate_key()))
    for value in (token, stored):
        r = cliente.post(f"/webhook/{tenant['slug']}/evolution", json=payload(),
                         headers={'X-Webhook-Token': value})
        assert r.status_code == 403
    with pytest.raises(ValueError):
        db.evolution_callback_token(tenant['id'])


def test_abortar_preserva_atual_e_nao_reabre_query(cliente, tenant, monkeypatch):
    from cryptography.fernet import Fernet
    import crypto
    monkeypatch.setattr(crypto, '_fernet', Fernet(Fernet.generate_key()))
    db.update_tenant(tenant['slug'], whatsapp_provider='evolution')
    first = db.stage_webhook_rotation(tenant['id'])
    db.finalize_webhook_rotation(tenant['id'], first)
    second = db.stage_webhook_rotation(tenant['id'])
    db.abort_webhook_rotation(tenant['id'], second)
    assert db.evolution_callback_token(tenant['id']) == first
    assert db.webhook_rotation_state(tenant['id'])['header_only'] == 1


def test_diagnostico_so_revela_presenca_e_correspondencia(cliente, tenant, monkeypatch):
    token = tenant['webhook_token']
    db.update_tenant(tenant['slug'], whatsapp_provider='evolution', evolution_instance='tenant-ficticio')
    async def conectado(*a, **k):
        return {'ok': True, 'connected': True}
    async def info(*a, **k):
        return {'ok': True}
    async def callback(instance, expected_token=''):
        assert expected_token == token
        return {'ok': True, 'url': 'https://testserver/webhook/'+tenant['slug']+'/evolution',
                'enabled': True, 'has_auth_header': True, 'has_query': False,
                'auth_header_matches': True, 'events': ['MESSAGES_UPSERT']}
    monkeypatch.setattr(config, 'EVOLUTION_API_URL', 'https://evolution.test')
    monkeypatch.setattr(config, 'EVOLUTION_API_KEY', 'ficticia')
    monkeypatch.setattr(main.wa, 'check_connection', conectado)
    monkeypatch.setattr(evo, 'get_instance_info', info)
    monkeypatch.setattr(evo, 'get_webhook', callback)
    r = cliente.get('/dashboard/api/whatsapp/diagnose', headers={'X-Dashboard-Token': tenant['dashboard_token']})
    assert r.status_code == 200
    assert token not in r.text
    assert r.json()['webhook']['confere'] is True
    assert r.json()['webhook']['token_em_header'] is True
    assert r.json()['webhook']['token_em_url'] is False
