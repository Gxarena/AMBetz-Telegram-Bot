import os
from dotenv import load_dotenv

load_dotenv()  # Loads variables from .env if present

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET")
PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")

# Optionally define PayPal API endpoints (sandbox vs. production)
PAYPAL_API_BASE = (
    "https://api-m.sandbox.paypal.com" 
    if ENVIRONMENT == "development" 
    else "https://api-m.paypal.com"
)
