from __future__ import annotations
import logging
import httpx
import config
import database as db

logger = logging.getLogger(__name__)

MP_API = "https://api.mercadopago.com"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": "",
    }


def _configured() -> bool:
    return bool(config.MP_ACCESS_TOKEN)


MP_PLANS = {
    "mensal":    {"label": "Mensal",    "frequency": 1,  "amount": 199.00},
    "semestral": {"label": "Semestral", "frequency": 6,  "amount": 1014.00},
    "anual":     {"label": "Anual",     "frequency": 12, "amount": 1788.00},
}


def create_subscription(tenant: dict, plan: str = "mensal") -> str:
    """Cria uma assinatura no Mercado Pago e retorna a URL de pagamento (init_point)."""
    base = config.BASE_URL
    setup_token = tenant.get("setup_token", "")
    p = MP_PLANS.get(plan, MP_PLANS["mensal"])

    payload = {
        "reason": f"Consultório Inteligente — Plano {p['label']}",
        "external_reference": setup_token,
        "auto_recurring": {
            "frequency": p["frequency"],
            "frequency_type": "months",
            "transaction_amount": p["amount"],
            "currency_id": "BRL",
        },
        "back_url": f"{base}/onboarding/sucesso?token={setup_token}&paid=1",
        "status": "pending",
    }
    db.update_tenant(tenant["slug"], plan=plan)
    logger.info(f"[mp] Criando assinatura para {tenant['slug']} — plano {p['label']} R${p['amount']}")

    # Adicionar e-mail do pagador se disponível
    if tenant.get("email"):
        payload["payer_email"] = tenant["email"]

    with httpx.Client(timeout=15) as client:
        r = client.post(
            f"{MP_API}/preapproval",
            json=payload,
            headers=_headers(),
        )
        r.raise_for_status()
        data = r.json()

    sub_id = data.get("id", "")
    init_point = data.get("init_point", "")

    if sub_id:
        db.update_tenant(tenant["slug"], mp_subscription_id=sub_id)
        logger.info(f"[mp] Assinatura criada: {sub_id} para {tenant['slug']}")

    return init_point


def handle_webhook(data: dict) -> dict:
    """Processa notificação do Mercado Pago e ativa/suspende o tenant."""
    action = data.get("action", "")
    sub_id = data.get("data", {}).get("id", "")

    if not sub_id:
        return {"received": True}

    logger.info(f"[mp] Webhook recebido: action={action} sub_id={sub_id}")

    # Buscar detalhes da assinatura na API do MP
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(
                f"{MP_API}/preapproval/{sub_id}",
                headers=_headers(),
            )
            r.raise_for_status()
            preapproval = r.json()
    except Exception as e:
        logger.warning(f"[mp] Erro ao buscar assinatura {sub_id}: {e}")
        return {"received": True}

    status = preapproval.get("status", "")
    external_ref = preapproval.get("external_reference", "")

    logger.info(f"[mp] Assinatura {sub_id}: status={status} ref={external_ref}")

    # Buscar tenant pelo setup_token (external_reference) ou mp_subscription_id
    tenant = None
    if external_ref:
        tenant = db.get_tenant_by_setup_token(external_ref)
    if not tenant:
        tenant = _get_tenant_by_mp_sub(sub_id)

    if not tenant:
        logger.warning(f"[mp] Tenant não encontrado para sub={sub_id}")
        return {"received": True}

    if status == "authorized":
        db.update_tenant(tenant["slug"], status="active", mp_subscription_id=sub_id)
        logger.info(f"[mp] Tenant {tenant['slug']} ativado via MP")
    elif status in ("cancelled", "paused"):
        if db.is_tenant_exempt(tenant):
            logger.info(f"[mp] Tenant {tenant['slug']} isento (free_until) — suspensão ignorada")
        else:
            db.update_tenant(tenant["slug"], status="suspended")
            logger.info(f"[mp] Tenant {tenant['slug']} suspenso via MP (status={status})")

    return {"received": True}


def _get_tenant_by_mp_sub(sub_id: str) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE mp_subscription_id = ?", (sub_id,)
        ).fetchone()
    return dict(row) if row else None


def get_manage_url(tenant: dict) -> str | None:
    """Retorna URL para o cliente gerenciar a assinatura no MP."""
    sub_id = tenant.get("mp_subscription_id", "")
    if not sub_id:
        return None
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{MP_API}/preapproval/{sub_id}", headers=_headers())
            r.raise_for_status()
            data = r.json()
        return data.get("init_point")
    except Exception as e:
        logger.warning(f"[mp] Erro ao buscar URL de gestão: {e}")
        return None
