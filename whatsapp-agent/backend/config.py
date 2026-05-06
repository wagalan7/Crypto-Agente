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
BASE_URL = os.getenv("BASE_URL", "https://agente-atendimento-production.up.railway.app")

GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

STRIPE_SECRET_KEY      = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET  = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID        = os.getenv("STRIPE_PRICE_ID", "")

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
