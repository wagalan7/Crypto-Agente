import os
from dotenv import load_dotenv

load_dotenv()

PORT = int(os.getenv("PORT", "8001"))
DATA_DIR = os.getenv("DATA_DIR", ".")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "consultorio")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")

WHATSAPP_PROVIDER = os.getenv("WHATSAPP_PROVIDER", "evolution")  # evolution | twilio | mock

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./consultorio.db")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

CLINIC_NAME = os.getenv("CLINIC_NAME", "Consultório de Psicologia")
PSYCHOLOGIST_NAME = os.getenv("PSYCHOLOGIST_NAME", "Dra. Ana")
SESSION_DURATION_MINUTES = int(os.getenv("SESSION_DURATION_MINUTES", "50"))

WORKING_DAYS = [0, 1, 2, 3, 4]  # Seg=0 ... Sex=4
WORKING_HOURS_START = int(os.getenv("WORKING_HOURS_START", "7"))
WORKING_HOURS_END = int(os.getenv("WORKING_HOURS_END", "21"))

MASTER_KEY = os.getenv("MASTER_KEY", "")
BASE_URL = os.getenv("BASE_URL", "https://agenteconsultorio.com.br")

GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

STRIPE_SECRET_KEY          = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET      = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID            = os.getenv("STRIPE_PRICE_ID", "")           # legado
STRIPE_PRICE_MENSAL        = os.getenv("STRIPE_PRICE_MENSAL", "")       # R$199/mês
STRIPE_PRICE_SEMESTRAL     = os.getenv("STRIPE_PRICE_SEMESTRAL", "")    # R$1.014 a cada 6m
STRIPE_PRICE_ANUAL         = os.getenv("STRIPE_PRICE_ANUAL", "")

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# ── Observabilidade ───────────────────────────────────────────────────────────
SENTRY_DSN = os.getenv("SENTRY_DSN", "")

# ── Backup (S3-compatible: Cloudflare R2, Backblaze B2, AWS S3) ──────────────
BACKUP_S3_BUCKET            = os.getenv("BACKUP_S3_BUCKET", "")
BACKUP_S3_ENDPOINT_URL      = os.getenv("BACKUP_S3_ENDPOINT_URL", "")   # ex: https://<account>.r2.cloudflarestorage.com
BACKUP_S3_ACCESS_KEY_ID     = os.getenv("BACKUP_S3_ACCESS_KEY_ID", "")
BACKUP_S3_SECRET_ACCESS_KEY = os.getenv("BACKUP_S3_SECRET_ACCESS_KEY", "")
BACKUP_S3_REGION            = os.getenv("BACKUP_S3_REGION", "auto")
BACKUP_RETENTION_DAYS       = int(os.getenv("BACKUP_RETENTION_DAYS", "30"))

# ── E-mail transacional (SMTP) ────────────────────────────────────────────────
# Deixe SMTP_HOST em branco para desabilitar o envio de e-mails.
SMTP_HOST     = os.getenv("SMTP_HOST", "")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM     = os.getenv("SMTP_FROM", SMTP_USER)   # "Nome <email>" ou só email
SMTP_USE_SSL  = os.getenv("SMTP_USE_SSL", "0")      # "1" para porta 465 (SSL direto)
