"""
Central configuration for the JourneyHealth Adapter.
All values are read from environment variables (via .env).
Import this module instead of calling os.getenv() directly.
"""
import os
from dotenv import load_dotenv

# Load .env from the project root (the directory where this file lives)
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

# ── AWS / Bedrock ──────────────────────────────────────────────────────────────
AWS_REGION = os.getenv("AWS_REGION")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID")
RESPONSE_MAX_TOKENS = int(os.getenv("RESPONSE_MAX_TOKENS"))
RESPONSE_TEMPERATURE = float(os.getenv("RESPONSE_TEMPERATURE"))

# ── Keycloak ───────────────────────────────────────────────────────────────────
KEYCLOAK_URL = os.getenv("KEYCLOAK_URL")
KEYCLOAK_CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID")

# ── Journey Health API ─────────────────────────────────────────────────────────
JOURNEY_WEAKLINK_API_URL = os.getenv("JOURNEY_WEAKLINK_API_URL")
JOURNEY_SUMMARY_API_BASE_URL = os.getenv("JOURNEY_SUMMARY_API_BASE_URL")
JOURNEY_HEALTH_RANGE_TYPE = os.getenv("JOURNEY_HEALTH_RANGE_TYPE")

# ── Credentials ────────────────────────────────────────────────────────────────
USERNAME = os.getenv("WM_USERNAME")
PASSWORD = os.getenv("WM_PASSWORD")
