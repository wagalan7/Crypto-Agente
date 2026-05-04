from __future__ import annotations
import re
import database as db


def slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[àáâãä]", "a", s)
    s = re.sub(r"[èéêë]", "e", s)
    s = re.sub(r"[ìíîï]", "i", s)
    s = re.sub(r"[òóôõö]", "o", s)
    s = re.sub(r"[ùúûü]", "u", s)
    s = re.sub(r"[ç]", "c", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def create_tenant(
    name: str,
    psychologist_name: str = "Psicóloga",
    working_hours_start: int = 7,
    working_hours_end: int = 21,
    session_minutes: int = 50,
    slug: str | None = None,
) -> dict:
    slug = slug or slugify(name)
    existing = db.get_tenant(slug)
    if existing:
        raise ValueError(f"Slug '{slug}' já está em uso.")
    tenant_id = db.create_tenant(
        slug=slug,
        name=name,
        psychologist_name=psychologist_name,
        working_hours_start=working_hours_start,
        working_hours_end=working_hours_end,
        session_minutes=session_minutes,
    )
    return db.get_tenant_by_id(tenant_id)


def get_tenant_or_404(slug: str) -> dict:
    tenant = db.get_tenant(slug)
    if not tenant:
        raise LookupError(f"Consultório '{slug}' não encontrado.")
    return tenant


def configure_whatsapp(slug: str, provider: str, **kwargs) -> dict:
    allowed_providers = {"evolution", "twilio", "mock"}
    if provider not in allowed_providers:
        raise ValueError(f"Provider inválido. Use: {allowed_providers}")
    db.update_tenant(slug, whatsapp_provider=provider, **kwargs)
    return db.get_tenant(slug)
