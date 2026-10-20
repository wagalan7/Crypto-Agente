"""Seed FICTÍCIO da prévia do E08B: dois consultórios (psicologia e nutrição)
com agenda de hoje. Nomes e telefones inventados; nenhum dado real."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import database as db

db.init_db()
hoje = datetime.now(ZoneInfo("America/Sao_Paulo")).replace(tzinfo=None, minute=0, second=0, microsecond=0)
for slug, nome, seg, token in (
    ("consultorio-ficticio", "Consultório Fictício", "psicologia", "previa-e08b-psi"),
    ("nutri-ficticio", "Nutrição Fictícia", "nutricao", "previa-e08b-nutri"),
):
    if not db.get_tenant(slug):
        db.create_tenant(slug=slug, name=nome, psychologist_name="Profissional Fictícia",
                         working_hours_start=8, working_hours_end=20)
    db.update_tenant(slug, segment=seg, dashboard_token=token, status="active", active=1,
                     ai_narrative_enabled=1, whatsapp_provider="mock")
    t = db.get_tenant(slug)
    for i, h in enumerate((9, 11, 14, 16, 18)):
        db.create_appointment(t["id"], f"Paciente Fictício {chr(65 + i)}", f"55119000001{i:02d}",
                              hoje.replace(hour=h) + timedelta(days=0 if i < 3 else 1))
print("seed ok")
