"""Contrato Automático — geração de PDF, template versionado e aceite eletrônico.

Fase 1 (fundação): gerar o PDF do contrato a partir de um template com
placeholders, criar a instância de contrato de um paciente e produzir o PDF
final assinado (aceite eletrônico próprio) com trilha de auditoria.

Assinatura = aceite eletrônico simples (MP 2.200-2/2001 + Marco Civil): o
paciente lê o contrato numa página hospedada, marca "Li e concordo", informa
nome + CPF e confirma. Capturamos data/hora (BRT), IP, user-agent e um hash
SHA-256 do conteúdo aceito — a trilha que dá validade probatória ao aceite.

Nada aqui toca a agenda/execução. É aditivo e fica atrás da flag do consultório.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config as _cfg
import database as db

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("America/Sao_Paulo")
_CONTRACTS_DIR = os.path.join(_cfg.DATA_DIR, "contracts")


# ── Template padrão de fábrica ──────────────────────────────────────────────────
# Texto base de um contrato de prestação de serviços de psicoterapia. A psicóloga
# pode editar/versionar depois (Fase 3). Placeholders no formato {{campo}}.
DEFAULT_TEMPLATE_BODY = """# Contrato de Prestação de Serviços de Psicologia

**CONTRATADA:** {{psicologa}} — {{consultorio}}
**CONTRATANTE:** {{nome}}
**CPF:** {{cpf}}
**Endereço:** {{endereco}}
**Telefone:** {{telefone}}

## 1. Objeto
O presente contrato tem por objeto a prestação de serviços de psicoterapia pela
CONTRATADA ao(à) CONTRATANTE, em sessões previamente agendadas.

## 2. Sessões e horários
As sessões têm duração previamente combinada e ocorrem em dia e horário
agendados entre as partes. Remarcações devem ser comunicadas com antecedência.

## 3. Valor e pagamento
O valor de cada sessão e a forma de pagamento são os combinados entre as partes,
podendo ser reajustados mediante aviso prévio. Pagamentos, preferencialmente, via
Pix — **Chave Pix:** {{chave_pix}}.

## 4. Faltas e cancelamentos
A ausência não comunicada com a antecedência combinada poderá ser cobrada,
conforme política do consultório informada ao(à) CONTRATANTE.

## 5. Sigilo profissional
A CONTRATADA compromete-se a manter sigilo sobre todo o conteúdo das sessões,
nos termos do Código de Ética Profissional do Psicólogo, ressalvadas as hipóteses
legais de quebra de sigilo.

## 6. Proteção de dados (LGPD)
Os dados pessoais do(a) CONTRATANTE serão tratados exclusivamente para a
finalidade deste atendimento, nos termos da Lei nº 13.709/2018 (LGPD).

## 7. Aceite
Ao assinar eletronicamente este documento, o(a) CONTRATANTE declara ter lido e
concordado integralmente com as cláusulas acima.

Data: {{data}}
"""


# Linha em branco para campos que serão preenchidos manualmente / mais tarde.
_FILL = "_______________"


def _placeholders(tenant: dict, contract: dict, patient: dict | None) -> dict:
    """Mapa de substituição dos {{campos}} do template.

    Campos do paciente/consultório resolvem sozinhos; os "dados do atendimento"
    (valor, dia, horário) e a chave PIX vêm, respectivamente, do que a psicóloga
    preencher no contrato e do cadastro do consultório. Campo vazio vira uma
    linha ('____') em vez de sumir — assim o contrato serve como formulário."""
    patient = patient or {}
    contract = contract or {}
    consultorio = tenant.get("name") or "Consultório"
    psicologa = tenant.get("psychologist_name") or "Psicóloga"
    hoje = datetime.now(_TZ).strftime("%d/%m/%Y")

    def v(x, default="—"):
        x = (x or "").strip() if isinstance(x, str) else (x or "")
        return str(x).strip() or default

    nome = (contract.get("patient_name") or patient.get("name") or "").strip()
    return {
        "nome": nome or "—",
        "nome_paciente": nome or "—",
        "cpf": v(patient.get("cpf")),
        "endereco": v(patient.get("address")),
        "telefone": v(contract.get("phone") or patient.get("phone")),
        "email": v(patient.get("email")),
        "data": hoje,
        "consultorio": consultorio,
        "psicologa": psicologa,
        # PIX sempre do cadastro do consultório (regra: usar a chave que ele cadastrou).
        "chave_pix": v(tenant.get("pix_key"), _FILL),
        "titular_pix": v(tenant.get("pix_name"), _FILL),
        # Dados do atendimento — preenchidos pela psicóloga (vazio → linha p/ preencher).
        "valor": v(contract.get("session_value"), _FILL),
        "dia": v(contract.get("session_day"), _FILL),
        "horario": v(contract.get("session_time"), _FILL),
        "modalidade_pagamento": v(contract.get("payment_mode"), _FILL),
        "modalidade_atendimento": v(contract.get("attendance_mode"), _FILL),
        # Assinatura da CONTRATADA (psicóloga). Preenchida quando ela assina (Fase B);
        # senão, uma linha para assinatura.
        "assinatura_contratada": (
            f"Assinado eletronicamente por {v(contract.get('psy_signer_name'), psicologa)} "
            f"em {v(contract.get('psy_signed_at'))}"
            if contract.get("psy_signed_at") else _FILL
        ),
    }


def render_body(body: str, ctx: dict) -> str:
    """Substitui {{campo}} pelos valores do contexto. Placeholders desconhecidos
    são deixados em branco (nunca vazam '{{...}}' para o PDF)."""
    def repl(m):
        key = m.group(1).strip().lower()
        return str(ctx.get(key, ""))
    return re.sub(r"\{\{\s*([a-zA-Z_]+)\s*\}\}", repl, body or "")


def body_to_html(rendered_body: str) -> str:
    """Converte o corpo (markdown simples, JÁ com placeholders resolvidos) em
    HTML seguro para a prévia na página de assinatura. Escapa tudo primeiro e só
    depois aplica # títulos, **negrito** e listas — nunca injeta HTML do template."""
    def esc(t: str) -> str:
        return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    out = []
    for raw in (rendered_body or "").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            out.append("")
            continue
        if line.startswith("# "):
            out.append(f"<h2>{esc(line[2:].strip())}</h2>")
        elif line.startswith("## "):
            out.append(f"<h3>{esc(line[3:].strip())}</h3>")
        elif line.lstrip().startswith(("- ", "* ")):
            out.append(f"<li>{esc(line.lstrip()[2:].strip())}</li>")
        else:
            txt = esc(line)
            txt = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", txt)
            out.append(f"<p>{txt}</p>")
    return "\n".join(out)


# ── Geração de PDF (reportlab) ──────────────────────────────────────────────────

def _build_pdf_bytes(title: str, rendered_body: str,
                     sign_meta: dict | None = None) -> bytes:
    """Renderiza o corpo (markdown simples) em PDF. Se sign_meta for passado,
    acrescenta o carimbo de aceite eletrônico com a trilha de auditoria."""
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    HRFlowable)

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=22 * mm, rightMargin=22 * mm,
        topMargin=20 * mm, bottomMargin=20 * mm,
        title=title,
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=15, spaceAfter=8)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=12, spaceBefore=8, spaceAfter=4)
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=10.5, leading=15, spaceAfter=6)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8.5, leading=12, textColor=colors.grey)

    def esc(t: str) -> str:
        # Escapa & < > e converte **negrito** em <b>…</b> (markdown simples).
        t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        return t

    story = []
    for raw in (rendered_body or "").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            story.append(Spacer(1, 4))
            continue
        if line.startswith("# "):
            story.append(Paragraph(esc(line[2:].strip()), h1))
        elif line.startswith("## "):
            story.append(Paragraph(esc(line[3:].strip()), h2))
        elif line.lstrip().startswith(("- ", "* ")):
            story.append(Paragraph("• " + esc(line.lstrip()[2:].strip()), body))
        else:
            story.append(Paragraph(esc(line), body))

    if sign_meta:
        story.append(Spacer(1, 10))
        story.append(HRFlowable(width="100%", thickness=0.6, color=colors.grey))
        story.append(Spacer(1, 6))
        story.append(Paragraph("Assinatura eletrônica (aceite)", h2))
        linhas = [
            f"<b>Assinado por:</b> {esc(sign_meta.get('signer_name',''))}",
            f"<b>CPF:</b> {esc(sign_meta.get('signer_cpf',''))}",
            f"<b>Data/hora (BRT):</b> {esc(sign_meta.get('signed_at_br',''))}",
            f"<b>IP:</b> {esc(sign_meta.get('sign_ip',''))}",
            f"<b>Dispositivo:</b> {esc(sign_meta.get('sign_user_agent',''))}",
            f"<b>Código de verificação (SHA-256):</b> {esc(sign_meta.get('sign_hash',''))}",
        ]
        for l in linhas:
            story.append(Paragraph(l, small))
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            "Documento assinado eletronicamente nos termos da MP 2.200-2/2001 e da "
            "Lei nº 12.965/2014 (Marco Civil da Internet). A integridade pode ser "
            "verificada pelo código acima.", small))

    doc.build(story)
    return buf.getvalue()


def compute_hash(*parts: str) -> str:
    """SHA-256 hex de uma concatenação canônica (trilha de integridade)."""
    h = hashlib.sha256()
    h.update("\u0000".join(p or "" for p in parts).encode("utf-8"))
    return h.hexdigest()


def _pdf_path(contract_id: int) -> str:
    os.makedirs(_CONTRACTS_DIR, exist_ok=True)
    return os.path.join(_CONTRACTS_DIR, f"contract_{contract_id}.pdf")


# ── API de alto nível ───────────────────────────────────────────────────────────

def get_or_create_default_template(tenant_id: int) -> dict:
    """Garante que o consultório tem um template vigente (cria o padrão se não)."""
    tpl = db.get_active_contract_template(tenant_id)
    if tpl:
        return tpl
    return db.create_contract_template(tenant_id, DEFAULT_TEMPLATE_BODY)


def create_contract_for_patient(tenant: dict, phone: str, patient_name: str = "",
                                fields: dict | None = None,
                                psy_sign: bool = False) -> dict:
    """Cria uma instância de contrato 'pendente' para o paciente, usando o
    template vigente. Retorna o contrato criado (com token). NÃO envia nada —
    o envio é responsabilidade da Fase 2. expires_at é calculado a partir da
    config do consultório (expire_days) só quando o contrato for enviado; aqui
    deixamos nulo (pendente ainda não tem relógio correndo).

    `fields` (opcional) traz os dados do atendimento que a psicóloga preencheu
    no painel: session_value, session_day, session_time, payment_mode,
    attendance_mode. Se `psy_sign=True`, registra também a assinatura eletrônica
    da CONTRATADA (a psicóloga está autenticada no painel) — nome do consultório
    + carimbo de data/hora BRT — que aparece no PDF junto ao aceite do paciente."""
    tenant_id = tenant["id"]
    template = get_or_create_default_template(tenant_id)
    token = secrets.token_urlsafe(24)
    if not patient_name:
        p = db.get_patient(tenant_id, phone)
        patient_name = (p.get("name") if p else "") or ""

    f = dict(fields or {})
    if psy_sign:
        psy_name = (tenant.get("psychologist_name") or tenant.get("name") or "Psicóloga").strip()
        f["psy_signer_name"] = psy_name
        f["psy_signed_at"] = datetime.now(_TZ).strftime("%d/%m/%Y %H:%M")

    cid = db.create_contract(tenant_id, phone, patient_name, template, token, fields=f)
    logger.info(f"[{tenant.get('slug')}] Contrato #{cid} criado (pendente) p/ {phone}"
                + (" [assinado pela CONTRATADA]" if psy_sign else ""))
    return db.get_contract(cid)


def render_contract_pdf(tenant: dict, contract: dict, sign_meta: dict | None = None) -> bytes:
    """Gera os bytes do PDF de um contrato (com ou sem carimbo de assinatura)."""
    template = None
    for t in db.list_contract_templates(tenant["id"]):
        if t["version"] == contract.get("template_version"):
            template = t
            break
    template = template or db.get_active_contract_template(tenant["id"]) \
        or {"title": "Contrato", "body": DEFAULT_TEMPLATE_BODY}
    patient = db.get_patient(tenant["id"], contract.get("phone", ""))
    ctx = _placeholders(tenant, contract, patient)
    rendered = render_body(template.get("body") or DEFAULT_TEMPLATE_BODY, ctx)
    return _build_pdf_bytes(template.get("title") or "Contrato", rendered, sign_meta)


def sign_contract(tenant: dict, contract: dict, signer_name: str, signer_cpf: str,
                  sign_ip: str, sign_user_agent: str) -> dict:
    """Efetiva o aceite: calcula o hash, gera o PDF assinado, salva em disco e
    grava o status/trilha no banco. Retorna o contrato atualizado.

    Idempotente-seguro: se já estiver assinado, apenas devolve o contrato."""
    if contract.get("status") == "assinado":
        return contract
    now = datetime.now(_TZ)
    signed_at_iso = now.isoformat()
    signed_at_br = now.strftime("%d/%m/%Y %H:%M")

    # Hash de integridade sobre o conteúdo aceito (corpo já renderizado + dados).
    base_pdf_note = f"contract={contract['id']};version={contract.get('template_version')}"
    sign_hash = compute_hash(base_pdf_note, signer_name, signer_cpf, signed_at_iso)

    sign_meta = {
        "signer_name": signer_name,
        "signer_cpf": signer_cpf,
        "signed_at_br": signed_at_br,
        "sign_ip": sign_ip,
        "sign_user_agent": (sign_user_agent or "")[:180],
        "sign_hash": sign_hash,
    }
    pdf_bytes = render_contract_pdf(tenant, contract, sign_meta=sign_meta)
    path = _pdf_path(contract["id"])
    with open(path, "wb") as f:
        f.write(pdf_bytes)

    db.mark_contract_signed(
        contract["id"], signer_name=signer_name, signer_cpf=signer_cpf,
        sign_ip=sign_ip, sign_user_agent=(sign_user_agent or "")[:180],
        sign_hash=sign_hash, pdf_path=path, signed_at=signed_at_iso,
    )
    logger.info(f"[{tenant.get('slug')}] Contrato #{contract['id']} ASSINADO por "
                f"{signer_name} (hash={sign_hash[:12]}…)")
    return db.get_contract(contract["id"])


def compute_expires_at(tenant_id: int, from_dt: datetime | None = None) -> str:
    """Data/hora de expiração a partir de agora + expire_days da config."""
    cfg = db.get_contract_settings(tenant_id)
    base = from_dt or datetime.now(_TZ)
    return (base + timedelta(days=int(cfg.get("expire_days", 7)))).isoformat()


# ── Fase 2: envio via WhatsApp + reenvio ────────────────────────────────────────

def public_base_url() -> str:
    """Base pública p/ montar o link de assinatura. Override por env
    CONTRACT_BASE_URL; senão usa config.BASE_URL (domínio do app)."""
    return (os.getenv("CONTRACT_BASE_URL", "") or _cfg.BASE_URL or "").rstrip("/")


def sign_link(token: str) -> str:
    return f"{public_base_url()}/contrato/{token}"


def _sign_message(tenant: dict, contract: dict, is_reminder: bool = False,
                  reminder_ordinal: int = 1) -> str:
    psic = tenant.get("psychologist_name") or "sua psicóloga"
    nome = (contract.get("patient_name") or "").split()[0] if contract.get("patient_name") else ""
    saud = f"Oi, {nome}! 😊" if nome else "Oi! 😊"
    link = sign_link(contract["token"])
    if is_reminder:
        lembr = "um lembrete rápido" if reminder_ordinal == 1 else "mais um lembrete"
        return (
            f"{saud} Passando só com {lembr}: seu contrato de atendimento com "
            f"{psic} ainda está aguardando sua assinatura. 💖\n\n"
            f"É rapidinho, direto pelo link (leia e assine no celular):\n{link}\n\n"
            f"Qualquer dúvida, é só me chamar por aqui!"
        )
    return (
        f"{saud} Para deixar tudo certinho antes das sessões, {psic} preparou o "
        f"contrato de atendimento. É rápido: leia e assine eletronicamente pelo "
        f"link abaixo (dá pra fazer pelo celular). 💖\n\n{link}\n\n"
        f"Se tiver qualquer dúvida, estou por aqui!"
    )


async def send_contract(tenant: dict, contract: dict, wa, is_reminder: bool = False,
                        reminder_ordinal: int = 1) -> tuple[bool, str]:
    """Envia o link de assinatura pelo WhatsApp do consultório e, no 1º envio,
    marca status='enviado' + calcula expires_at. `wa` é o whatsapp_service
    (injetado p/ evitar import circular). Retorna (ok, motivo)."""
    msg = _sign_message(tenant, contract, is_reminder, reminder_ordinal)
    try:
        sent, reason = await wa.send_message_ex(tenant, contract["phone"], msg)
    except Exception as e:
        return False, f"exceção no envio: {e}"
    if sent and not is_reminder:
        expires = compute_expires_at(tenant["id"])
        db.mark_contract_sent(contract["id"], expires)
    return sent, reason


async def create_and_send(tenant: dict, phone: str, wa, patient_name: str = "") -> dict:
    """Cria (se preciso) e envia um contrato para o paciente. Retorna o contrato."""
    contract = create_contract_for_patient(tenant, phone, patient_name)
    await send_contract(tenant, contract, wa)
    return db.get_contract(contract["id"])


async def resend_contract(tenant: dict, contract: dict, wa) -> tuple[bool, str]:
    """Reenvia o contrato. Se já expirou/rejeitou, renova o relógio (novo
    expires_at) e volta o status para 'enviado'. Não cria contrato novo — mantém
    o mesmo token/histórico."""
    if contract.get("status") == "assinado":
        return False, "contrato já assinado"
    ok, reason = await send_contract(tenant, contract, wa)
    if ok:
        # Reset do relógio: reenviar dá sobrevida ao paciente.
        expires = compute_expires_at(tenant["id"])
        db.mark_contract_sent(contract["id"], expires)
    return ok, reason


# ── Fase 4: bloqueios (gates) — opt-in, OFF por padrão, fail-open ────────────
#
# Um consultório pode, opcionalmente, exigir contrato assinado antes de deixar
# o paciente agendar e/ou confirmar. É desligado por padrão (block_scheduling=0,
# block_confirmation=0) e, mesmo ligado, qualquer erro interno NUNCA bloqueia o
# paciente (fail-open) — a operação normal do consultório vem sempre em 1º lugar.

def _pending_sign_link(tenant_id: int, phone: str) -> str:
    """Link de assinatura de um contrato ainda em aberto (enviado/pendente) para
    este telefone, se houver. Usado para já mandar o link junto do bloqueio."""
    try:
        for c in db.get_contracts_for_phone(tenant_id, phone):
            if c.get("status") in ("enviado", "pendente") and c.get("token"):
                return sign_link(c["token"])
    except Exception:
        pass
    return ""


def _block_message(tenant: dict, phone: str, action: str) -> str:
    """Mensagem amigável pedindo a assinatura do contrato antes de prosseguir."""
    psic = (tenant.get("psychologist_name") or tenant.get("name") or "").strip()
    verbo = "agendar sua sessão" if action == "scheduling" else "confirmar sua presença"
    link = _pending_sign_link(tenant["id"], phone)
    msg = (f"Antes de {verbo}, precisamos do seu contrato de atendimento "
           f"assinado 📄\n\n")
    if link:
        msg += (f"É rapidinho, basta assinar por aqui:\n{link}\n\n"
                f"Assim que assinar, seguimos com seu atendimento normalmente 😊")
    else:
        msg += ("Vou providenciar o link de assinatura e já te envio. "
                "Assim que assinar, seguimos com seu atendimento 😊")
    if psic:
        msg += f"\n\n— {psic}"
    return msg


def _is_gated(tenant: dict, phone: str, which: str) -> tuple[bool, str]:
    """Coração dos bloqueios. Retorna (bloqueado, mensagem). which ∈
    {'scheduling','confirmation'}. Fail-open: qualquer exceção → não bloqueia."""
    try:
        st = db.get_contract_settings(tenant["id"])
        if not st.get("enabled"):
            return (False, "")
        flag = st.get("block_scheduling") if which == "scheduling" \
            else st.get("block_confirmation")
        if not flag:
            return (False, "")
        require_version = None
        if st.get("require_current_version"):
            tpl = db.get_active_contract_template(tenant["id"])
            require_version = int(tpl["version"]) if tpl else None
        if db.has_signed_contract(tenant["id"], phone, require_version):
            return (False, "")
        return (True, _block_message(tenant, phone, which))
    except Exception:
        logger.exception("contract gate fail-open (which=%s)", which)
        return (False, "")


def blocks_scheduling(tenant: dict, phone: str) -> tuple[bool, str]:
    """Deve bloquear o agendamento deste paciente por falta de contrato?"""
    return _is_gated(tenant, phone, "scheduling")


def blocks_confirmation(tenant: dict, phone: str) -> tuple[bool, str]:
    """Deve bloquear a confirmação de presença deste paciente?"""
    return _is_gated(tenant, phone, "confirmation")


# ── Fase 4b: auto-envio + trava SÓ para pacientes NOVOS ──────────────────────
#
# Cenário da Bruna: disparar o contrato automaticamente para pacientes NOVOS e
# travar até assinarem; pacientes JÁ EXISTENTES ficam intocados (nunca recebem,
# nunca são travados). Tudo atrás da flag auto_send_new (OFF por padrão) e do
# activation_cutoff (carimbo gravado ao ligar a flag). Fail-open: qualquer erro
# NUNCA trava o paciente.
#
# Como _execute_action do agente é SÍNCRONO, não há envio ativo aqui: o próprio
# retorno do agente (a mensagem de bloqueio) já leva o link de assinatura ao
# paciente. Basta garantir que o contrato exista (criação é I/O de banco, síncrono).

def _ensure_new_patient_contract(tenant: dict, phone: str) -> None:
    """Idempotente: se já existe um contrato em aberto (enviado/pendente) para o
    telefone, não faz nada (evita spam/duplicata). Senão cria um novo, já assinado
    pela CONTRATADA (psy_sign), e o marca como 'enviado' — o link chega ao paciente
    na própria resposta do agente. Os dados do atendimento (valor/dia/horário)
    ficam em branco: o auto-envio não os conhece; o paciente lê e assina, e a
    psicóloga pode complementar depois no painel."""
    try:
        for c in db.get_contracts_for_phone(tenant["id"], phone):
            if c.get("status") in ("enviado", "pendente"):
                return  # já tem link em aberto
        contract = create_contract_for_patient(tenant, phone, psy_sign=True)
        db.mark_contract_sent(contract["id"], compute_expires_at(tenant["id"]))
        logger.info(f"[{tenant.get('slug')}][{phone}] contrato auto-enviado "
                    f"(paciente novo) #{contract['id']}")
    except Exception:
        logger.exception("_ensure_new_patient_contract fail (phone=%s)", phone)


def enforce_new_patient(tenant: dict, phone: str, which: str) -> tuple[bool, str]:
    """Auto-envio + trava para pacientes NOVOS. Retorna (bloqueado, mensagem).
    which ∈ {'scheduling','confirmation'}.

    Liga só quando auto_send_new=1. Um paciente é 'novo' se NÃO está na lista de
    isenção congelada na ativação (ver db.patient_is_new). Para um novo que não assinou:
    garante o contrato enviado (idempotente) e devolve (True, msg_com_link).
    Pacientes existentes (< cutoff) e quem já assinou passam direto → (False, '').
    Fail-open: qualquer exceção → (False, '') (nunca trava o paciente)."""
    try:
        st = db.get_contract_settings(tenant["id"])
        if not st.get("auto_send_new"):
            return (False, "")
        activated = bool(st.get("activation_cutoff") or "")
        if not db.patient_is_new(tenant["id"], phone, activated):
            return (False, "")  # paciente existente (isento) → intocado
        require_version = None
        if st.get("require_current_version"):
            tpl = db.get_active_contract_template(tenant["id"])
            require_version = int(tpl["version"]) if tpl else None
        if db.has_signed_contract(tenant["id"], phone, require_version):
            return (False, "")  # já assinou → liberado
        _ensure_new_patient_contract(tenant, phone)
        return (True, _block_message(tenant, phone, which))
    except Exception:
        logger.exception("enforce_new_patient fail-open (which=%s)", which)
        return (False, "")


async def send_to_existing(tenant: dict, wa, dry_run: bool = False,
                           throttle_s: float = 1.2) -> dict:
    """Envia (uma vez) o contrato a TODOS os pacientes EXISTENTES — o snapshot de
    isenção congelado na ativação — SEM bloquear a agenda deles. O bloqueio segue
    valendo só para NOVOS (via enforce_new_patient); estes existentes recebem o
    link mas continuam agendando/confirmando normalmente.

    Idempotente: pula quem já assinou ou já tem contrato em aberto (enviado/
    pendente). Assim, rodar de novo não gera envio duplicado. Rate-limited
    (throttle_s entre envios) para não sobrecarregar a Evolution. Contrato é
    auto-assinado pela CONTRATADA (igual ao fluxo de novos), com valor/dia/horário
    em branco. `dry_run=True` só conta quem receberia, sem enviar nada.

    Só faz sentido depois da ativação (auto_send_new=1 + cutoff carimbado): se a
    feature não está ativada, não há snapshot e retorna zerado."""
    result = {"exempt_total": 0, "sent": 0, "skipped_signed": 0,
              "skipped_open": 0, "failed": 0, "dry_run": bool(dry_run),
              "details": []}
    try:
        st = db.get_contract_settings(tenant["id"])
        if not (st.get("auto_send_new") and (st.get("activation_cutoff") or "")):
            result["reason"] = "feature não ativada (sem snapshot)"
            return result
        phones = db.get_exempt_phones(tenant["id"])
        result["exempt_total"] = len(phones)
        for phone in phones:
            try:
                if db.has_signed_contract(tenant["id"], phone, None):
                    result["skipped_signed"] += 1
                    continue
                open_c = None
                for c in db.get_contracts_for_phone(tenant["id"], phone):
                    if c.get("status") in ("enviado", "pendente"):
                        open_c = c
                        break
                if open_c is not None:
                    result["skipped_open"] += 1
                    continue
                if dry_run:
                    result["sent"] += 1  # contaria como "enviaria"
                    continue
                contract = create_contract_for_patient(tenant, phone, psy_sign=True)
                ok, reason = await send_contract(tenant, contract, wa)
                if ok:
                    result["sent"] += 1
                else:
                    result["failed"] += 1
                    if len(result["details"]) < 20:
                        result["details"].append({"phone": phone, "reason": reason})
                if throttle_s:
                    await asyncio.sleep(throttle_s)
            except Exception as e:
                result["failed"] += 1
                if len(result["details"]) < 20:
                    result["details"].append({"phone": phone, "reason": str(e)})
                logger.exception("send_to_existing item fail (phone=%s)", phone)
        logger.info("[%s] send_to_existing: %s", tenant.get("slug"), result)
        return result
    except Exception:
        logger.exception("send_to_existing fail-open")
        result["reason"] = "exceção geral (fail-open)"
        return result
