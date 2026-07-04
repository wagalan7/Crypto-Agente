"""Presets de segmento (Track B — multi-segmento aditivo).

Fase 1: segmentos de saúde/bem-estar com dinâmica de agendamento parecida com
psicologia. Fase 2: serviços/negócios (advocacia, contabilidade, salão, barbearia,
pet shop, oficina) — mesma mecânica de agendamento, só muda a terminologia.
Psicologia é o comportamento BASE (byte-idêntico) — o preset dela mantém os
rótulos vazios pra que o código legado siga usando os textos originais.

Cada preset define os termos de UI/linguagem:
  - professional_label: como chamar o profissional (ex.: "Nutricionista")
  - client_noun:        como chamar o cliente (ex.: "paciente" / "cliente")
  - service_noun:       como chamar o serviço (ex.: "sessão" / "consulta")
  - business_type:      tipo de negócio (ex.: "consultório" / "clínica")
"""

SEGMENT_PRESETS: dict[str, dict[str, str]] = {
    # Psicologia = base. Rótulos vazios ⇒ mantém textos legados byte-idênticos.
    "psicologia": {
        "professional_label": "",
        "client_noun": "",
        "service_noun": "",
        "business_type": "",
        "display": "Psicologia",
    },
    "nutricao": {
        "professional_label": "Nutricionista",
        "client_noun": "paciente",
        "service_noun": "consulta",
        "business_type": "consultório",
        "display": "Nutrição",
    },
    "fisioterapia": {
        "professional_label": "Fisioterapeuta",
        "client_noun": "paciente",
        "service_noun": "sessão",
        "business_type": "clínica",
        "display": "Fisioterapia",
    },
    "fonoaudiologia": {
        "professional_label": "Fonoaudiólogo(a)",
        "client_noun": "paciente",
        "service_noun": "sessão",
        "business_type": "clínica",
        "display": "Fonoaudiologia",
    },
    "odontologia": {
        "professional_label": "Dentista",
        "client_noun": "paciente",
        "service_noun": "consulta",
        "business_type": "consultório",
        "display": "Odontologia",
    },
    "terapia": {
        "professional_label": "Terapeuta",
        "client_noun": "paciente",
        "service_noun": "sessão",
        "business_type": "espaço",
        "display": "Terapia / Psicanálise",
    },
    "estetica": {
        "professional_label": "Esteticista",
        "client_noun": "cliente",
        "service_noun": "atendimento",
        "business_type": "estúdio",
        "display": "Estética / Massoterapia",
    },
    # ── Fase 2: serviços/negócios (mesma dinâmica de agendamento) ──────────────
    "advocacia": {
        "professional_label": "Advogado(a)",
        "client_noun": "cliente",
        "service_noun": "consulta",
        "business_type": "escritório",
        "display": "Advocacia",
    },
    "contabilidade": {
        "professional_label": "Contador(a)",
        "client_noun": "cliente",
        "service_noun": "reunião",
        "business_type": "escritório",
        "display": "Contabilidade",
    },
    "salao": {
        "professional_label": "Profissional",
        "client_noun": "cliente",
        "service_noun": "atendimento",
        "business_type": "salão",
        "display": "Salão de beleza",
    },
    "barbearia": {
        "professional_label": "Barbeiro(a)",
        "client_noun": "cliente",
        "service_noun": "atendimento",
        "business_type": "barbearia",
        "display": "Barbearia",
    },
    "petshop": {
        "professional_label": "Profissional",
        "client_noun": "tutor",
        "service_noun": "atendimento",
        "business_type": "pet shop",
        "display": "Pet shop",
    },
    "oficina": {
        "professional_label": "Mecânico(a)",
        "client_noun": "cliente",
        "service_noun": "serviço",
        "business_type": "oficina",
        "display": "Oficina mecânica",
    },
}

_DEFAULTS = {
    "professional_label": "profissional",
    "client_noun": "cliente",
    "service_noun": "atendimento",
    "business_type": "estabelecimento",
}


def normalize_segment(segment: str | None) -> str:
    """Segmento canônico (minúsculo, sem espaços). Vazio ⇒ 'psicologia'."""
    s = (segment or "").strip().lower()
    return s if s in SEGMENT_PRESETS else "psicologia"


def preset_for(segment: str | None) -> dict[str, str]:
    """Preset do segmento (fallback psicologia)."""
    return SEGMENT_PRESETS[normalize_segment(segment)]


def _pluralize_pt(word: str) -> str:
    """Plural PT-BR simplificado o bastante pros nossos termos.

    sessão→sessões · consulta→consultas · atendimento→atendimentos.
    """
    w = (word or "").strip()
    if not w:
        return w
    if w.endswith("ão"):
        return w[:-2] + "ões"
    if w.endswith(("r", "z", "s")):
        return w + "es"
    if w.endswith("m"):
        return w[:-1] + "ns"
    return w + "s"


def resolve_terms(tenant: dict) -> dict[str, str]:
    """Termos efetivos de UI pra um tenant.

    Precedência: override explícito na coluna do tenant → preset do segmento →
    default genérico. Psicologia cai nos defaults de saúde (paciente/sessão),
    que é exatamente o que o dashboard legado já usa.
    """
    seg = normalize_segment(tenant.get("segment"))
    preset = SEGMENT_PRESETS[seg]

    def pick(col: str, key: str, legacy_default: str) -> str:
        override = (tenant.get(col) or "").strip()
        if override:
            return override
        pv = (preset.get(key) or "").strip()
        return pv or legacy_default

    service_noun = pick("service_noun", "service_noun", "sessão")
    return {
        "segment": seg,
        # psicologia legado: "psicóloga"/nome; genérico: rótulo do preset
        "professional_label": pick("professional_label", "professional_label",
                                   "profissional" if seg != "psicologia" else "psicólogo(a)"),
        "client_noun": pick("client_noun", "client_noun", "paciente"),
        "service_noun": service_noun,
        "service_noun_plural": _pluralize_pt(service_noun),
        "business_type": pick("business_type", "business_type", "consultório"),
    }
