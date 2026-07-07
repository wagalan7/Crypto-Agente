"""
Serviço de e-mail transacional via SMTP (stdlib, sem dependências externas).

Configuração via variáveis de ambiente:
  SMTP_HOST       ex: smtp.gmail.com
  SMTP_PORT       ex: 587  (TLS/STARTTLS) ou 465 (SSL)
  SMTP_USER       ex: contato@seudominio.com
  SMTP_PASSWORD   senha do app (Gmail App Password, etc.)
  SMTP_FROM       ex: "Consultório Inteligente <contato@seudominio.com>"
  SMTP_USE_SSL    "1" para SSL direto (porta 465), caso contrário usa STARTTLS

Se SMTP_HOST não estiver configurado, os métodos logam um aviso e retornam False
silenciosamente — nunca lançam exceção.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import config

logger = logging.getLogger(__name__)

_SMTP_HOST     = os.getenv("SMTP_HOST", "").strip()
_SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
_SMTP_USER     = os.getenv("SMTP_USER", "").strip()
_SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
_SMTP_FROM     = os.getenv("SMTP_FROM", _SMTP_USER).strip()
_SMTP_USE_SSL  = os.getenv("SMTP_USE_SSL", "0").strip() == "1"


def _configured() -> bool:
    return bool(_SMTP_HOST and _SMTP_USER and _SMTP_PASSWORD)


def send_email(to: str, subject: str, html: str, text: str = "") -> bool:
    """Envia e-mail HTML (com fallback texto puro). Retorna True em caso de sucesso."""
    if not _configured():
        logger.debug(f"[email] SMTP não configurado — e-mail para {to} não enviado")
        return False

    if not to or "@" not in to:
        logger.warning(f"[email] Endereço inválido: {to!r}")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = _SMTP_FROM or _SMTP_USER
    msg["To"]      = to

    if text:
        msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        context = ssl.create_default_context()
        if _SMTP_USE_SSL:
            with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT, context=context) as server:
                server.login(_SMTP_USER, _SMTP_PASSWORD)
                server.sendmail(_SMTP_FROM or _SMTP_USER, [to], msg.as_bytes())
        else:
            with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as server:
                server.ehlo()
                server.starttls(context=context)
                server.login(_SMTP_USER, _SMTP_PASSWORD)
                server.sendmail(_SMTP_FROM or _SMTP_USER, [to], msg.as_bytes())
        logger.info(f"[email] ✓ Enviado para {to} — {subject!r}")
        return True
    except Exception as e:
        logger.warning(f"[email] ✗ Falha ao enviar para {to}: {e}")
        return False


# ── Templates de e-mail ───────────────────────────────────────────────────────

def _base_html(title: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{title}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #f9fafb; margin: 0; padding: 0; }}
    .wrapper {{ max-width: 580px; margin: 32px auto; background: #fff;
                border-radius: 16px; overflow: hidden;
                box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
    .header  {{ background: #E91E8C; padding: 32px 32px 24px; text-align: center; }}
    .header h1 {{ color: #fff; margin: 0; font-size: 22px; font-weight: 700; }}
    .header p  {{ color: rgba(255,255,255,.85); margin: 6px 0 0; font-size: 14px; }}
    .body    {{ padding: 28px 32px; color: #374151; line-height: 1.6; font-size: 15px; }}
    .body h2 {{ font-size: 17px; color: #111827; margin-top: 0; }}
    .btn     {{ display: inline-block; background: #E91E8C; color: #fff !important;
                text-decoration: none; padding: 13px 28px; border-radius: 10px;
                font-weight: 600; font-size: 15px; margin: 16px 0; }}
    .code-box{{ background: #f3f4f6; border-radius: 8px; padding: 12px 16px;
                font-family: monospace; font-size: 13px; color: #1f2937;
                word-break: break-all; margin: 12px 0; }}
    .tip     {{ background: #fdf2f8; border-left: 3px solid #E91E8C;
                padding: 12px 16px; border-radius: 0 8px 8px 0;
                font-size: 13px; color: #6b21a8; margin: 16px 0; }}
    .footer  {{ padding: 16px 32px 24px; font-size: 12px; color: #9ca3af;
                text-align: center; }}
  </style>
</head>
<body>
  <div class="wrapper">
    <div class="header">
      <h1>🤖 Consultório Inteligente</h1>
      <p>Agente de Atendimento WhatsApp para Psicólogas</p>
    </div>
    <div class="body">
      {body_html}
    </div>
    <div class="footer">
      Dúvidas? Fale conosco pelo WhatsApp: <a href="https://wa.me/5511968439527">wa.me/5511968439527</a><br/>
      <a href="{config.BASE_URL}">{config.BASE_URL}</a>
    </div>
  </div>
</body>
</html>"""


def send_welcome_email(email: str, name: str, setup_token: str) -> bool:
    """
    E-mail de boas-vindas enviado logo após o preenchimento do formulário.
    Contém o link de pagamento para não perder o acesso caso a aba seja fechada.
    """
    payment_url = f"{config.BASE_URL}/onboarding/pagamento?token={setup_token}"
    first_name = (name or "").split()[0] if name else "Olá"

    html_body = f"""
<h2>Olá, {first_name}! 🎉</h2>
<p>Seu cadastro no <strong>Consultório Inteligente</strong> foi recebido com sucesso.</p>
<p>Para ativar seu agente, finalize o pagamento clicando no botão abaixo:</p>
<p style="text-align:center">
  <a href="{payment_url}" class="btn">💳 Finalizar assinatura</a>
</p>
<p>Salve este e-mail — você pode voltar a esta página a qualquer momento.</p>
<div class="tip">
  💡 Após confirmar o pagamento, você receberá um novo e-mail com o link do seu painel e as instruções de configuração do WhatsApp.
</div>
<p>Qualquer dúvida, entre em contato pelo WhatsApp.</p>
<p>Abraços,<br/><strong>Equipe Consultório Inteligente</strong></p>
"""
    text_body = (
        f"Olá {first_name}!\n\n"
        f"Seu cadastro foi recebido. Para ativar seu agente acesse:\n{payment_url}\n\n"
        f"Dúvidas? wa.me/5511968439527"
    )
    return send_email(
        to=email,
        subject="🎉 Seu cadastro foi recebido — finalize sua assinatura",
        html=_base_html("Boas-vindas — Consultório Inteligente", html_body),
        text=text_body,
    )


def send_activation_email(
    email: str,
    name: str,
    slug: str,
    dashboard_token: str,
    setup_token: str,
    plan_label: str = "Mensal",
    expires_at: Optional[str] = None,
) -> bool:
    """
    E-mail de ativação enviado após confirmação de pagamento (Stripe ou MP).
    Contém: link do painel, URL do webhook, próximos passos.
    """
    dashboard_url = f"{config.BASE_URL}/dashboard/{slug}?token={dashboard_token}"
    webhook_url   = f"{config.BASE_URL}/webhook/{slug}"
    first_name    = (name or "").split()[0] if name else "Psicóloga"

    expires_html = ""
    if expires_at:
        try:
            from datetime import datetime as _dt
            d = _dt.fromisoformat(expires_at)
            expires_html = f"<p>📅 Próxima cobrança: <strong>{d.strftime('%d/%m/%Y')}</strong></p>"
        except Exception:
            pass

    html_body = f"""
<h2>Parabéns, {first_name}! Seu agente está ativo. 🚀</h2>
<p>Sua assinatura <strong>{plan_label}</strong> foi confirmada e seu agente já está pronto para usar.</p>
{expires_html}

<h2 style="margin-top:24px">📊 Seu Painel</h2>
<p>Acesse o painel para configurar horários, visualizar consultas e muito mais:</p>
<p style="text-align:center">
  <a href="{dashboard_url}" class="btn">🔗 Acessar meu Painel</a>
</p>
<div class="code-box">{dashboard_url}</div>
<div class="tip">⭐ Salve este link nos seus favoritos ou na tela inicial do celular para acesso rápido.</div>

<h2 style="margin-top:24px">📲 Configurar o WhatsApp</h2>
<p>Para conectar seu número, acesse a aba <strong>Configurações → WhatsApp (Z-API)</strong> no painel e informe as credenciais do seu Z-API.</p>
<p>O webhook do seu consultório é:</p>
<div class="code-box">{webhook_url}</div>

<h2 style="margin-top:24px">🆘 Precisa de ajuda?</h2>
<p>Fale com o suporte pelo WhatsApp: <a href="https://wa.me/5511968439527">wa.me/5511968439527</a></p>

<p>Sucesso nos atendimentos! 💜<br/><strong>Equipe Consultório Inteligente</strong></p>
"""
    text_body = (
        f"Parabéns {first_name}! Seu agente está ativo.\n\n"
        f"Painel: {dashboard_url}\n"
        f"Webhook: {webhook_url}\n\n"
        f"Dúvidas? wa.me/5511968439527"
    )
    return send_email(
        to=email,
        subject="✅ Seu agente está ativo! Acesse seu painel",
        html=_base_html("Agente Ativado — Consultório Inteligente", html_body),
        text=text_body,
    )


def send_link_recovery_email(
    email: str,
    name: str,
    slug: str,
    dashboard_token: str,
) -> bool:
    """
    E-mail de recuperação do link do painel (solicitado pelo usuário).
    """
    dashboard_url = f"{config.BASE_URL}/dashboard/{slug}?token={dashboard_token}"
    first_name    = (name or "").split()[0] if name else "Psicóloga"

    html_body = f"""
<h2>Olá, {first_name}!</h2>
<p>Você solicitou o link de acesso ao seu painel. Aqui está:</p>
<p style="text-align:center">
  <a href="{dashboard_url}" class="btn">🔗 Acessar meu Painel</a>
</p>
<div class="code-box">{dashboard_url}</div>
<div class="tip">⭐ Salve este link nos favoritos para não precisar solicitá-lo novamente.</div>
<p>Se você não fez essa solicitação, pode ignorar este e-mail.</p>
<p>— <strong>Equipe Consultório Inteligente</strong></p>
"""
    text_body = (
        f"Olá {first_name}!\n\n"
        f"Seu link de acesso ao painel:\n{dashboard_url}\n\n"
        f"Se não solicitou, ignore este e-mail."
    )
    return send_email(
        to=email,
        subject="🔗 Seu link de acesso ao Painel",
        html=_base_html("Recuperação de acesso — Consultório Inteligente", html_body),
        text=text_body,
    )


def send_reactivation_email(
    email: str,
    name: str,
    setup_token: str,
    status: str = "suspended",
) -> bool:
    """
    E-mail para quem recuperou o acesso mas está com pagamento pendente
    (cadastro suspenso ou que nunca finalizou o pagamento). Em vez do link do
    painel, leva à página de pagamento — o dashboard só é liberado após a
    assinatura ser confirmada. Permite que um cliente antigo reative o MESMO
    cadastro sem perder histórico.
    """
    payment_url = f"{config.BASE_URL}/onboarding/pagamento?token={setup_token}"
    first_name  = (name or "").split()[0] if name else "Olá"

    html_body = f"""
<h2>Que bom te ver de volta, {first_name}! 👋</h2>
<p>Encontramos o seu cadastro. Para reativar o seu agente de atendimento e voltar a
acessar o painel, falta apenas <strong>concluir o pagamento</strong> da assinatura.</p>
<p style="text-align:center">
  <a href="{payment_url}" class="btn">💳 Reativar minha assinatura</a>
</p>
<div class="code-box">{payment_url}</div>
<div class="tip">Assim que o pagamento for confirmado, seu painel é liberado
automaticamente e o agente volta a responder — com o mesmo cadastro de antes.</div>
<p>Se você não fez essa solicitação, pode ignorar este e-mail.</p>
<p>— <strong>Equipe Consultório Inteligente</strong></p>
"""
    text_body = (
        f"Olá {first_name}!\n\n"
        f"Encontramos seu cadastro. Há um pagamento pendente — conclua para reativar "
        f"seu agente e acessar o painel:\n{payment_url}\n\n"
        f"Assim que o pagamento for confirmado, o painel é liberado automaticamente.\n\n"
        f"Se não solicitou, ignore este e-mail."
    )
    return send_email(
        to=email,
        subject="💳 Reative seu acesso — Consultório Inteligente",
        html=_base_html("Reative seu acesso — Consultório Inteligente", html_body),
        text=text_body,
    )
