import stripe
import os
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from google.cloud import secretmanager

# Configure logging
logger = logging.getLogger(__name__)

class GCPStripeService:
    def __init__(self, project_id: str = None):
        """Initialize Stripe service with GCP Secret Manager integration"""
        self.project_id = project_id or os.getenv('GOOGLE_CLOUD_PROJECT')
        if not self.project_id:
            raise ValueError("GOOGLE_CLOUD_PROJECT environment variable is required")
        
        # Initialize Secret Manager client
        self.secret_client = secretmanager.SecretManagerServiceClient()
        
        # Get Stripe credentials from Secret Manager
        self.publishable_key = self._get_secret("stripe-publishable-key")
        self.secret_key = self._get_secret("stripe-secret-key")
        self.webhook_secret = self._get_secret("stripe-webhook-secret")
        self.price_id = self._get_secret("stripe-price-id")
        
        # Check if Stripe is configured
        self.is_configured = bool(self.secret_key)
        
        if self.is_configured:
            stripe.api_key = self.secret_key
            logger.info("GCP Stripe service initialized with Secret Manager")
        else:
            logger.warning("Stripe not configured - payment features will be disabled")
    
    def _get_secret(self, secret_name: str) -> str:
        """Get secret from GCP Secret Manager"""
        try:
            # Build the resource name of the secret version
            name = f"projects/{self.project_id}/secrets/{secret_name}/versions/latest"
            
            # Access the secret version
            response = self.secret_client.access_secret_version(request={"name": name})
            
            # Return the decoded payload
            return response.payload.data.decode("UTF-8")
        except Exception as e:
            logger.error(f"Error accessing secret {secret_name}: {e}")
            # Fallback to environment variables for development
            return os.getenv(secret_name.upper().replace('-', '_'))
    
    def create_payment_link(self, telegram_id: int, telegram_username: str = None) -> str:
        """Create a Stripe payment link for a user"""
        if not self.is_configured:
            raise ValueError("Stripe is not configured")
            
        try:
            # Create or retrieve customer
            customer = self.get_or_create_customer(telegram_id, telegram_username)
            
            # Create payment link
            payment_link = stripe.PaymentLink.create(
                line_items=[
                    {
                        "price": self.price_id,
                        "quantity": 1,
                    },
                ],
                metadata={
                    "telegram_id": str(telegram_id),
                    "telegram_username": telegram_username or "",
                    "source": "gcp-bot"
                }
            )
            
            logger.info(f"Payment link created for user {telegram_id}")
            return payment_link.url
            
        except Exception as e:
            logger.error(f"Error creating payment link: {e}")
            raise
    
    def get_or_create_customer(self, telegram_id: int, telegram_username: str = None) -> stripe.Customer:
        """Get existing customer or create new one"""
        if not self.is_configured:
            raise ValueError("Stripe is not configured")
            
        try:
            # Search for existing customer by telegram_id
            customers = stripe.Customer.search(
                query=f"metadata['telegram_id']:'{telegram_id}'"
            )
            
            if customers.data:
                return customers.data[0]
            
            # Create new customer if not found
            customer = stripe.Customer.create(
                metadata={
                    "telegram_id": str(telegram_id),
                    "telegram_username": telegram_username or "",
                    "source": "gcp-bot"
                }
            )
            
            logger.info(f"Customer created for telegram user {telegram_id}")
            return customer
            
        except Exception as e:
            logger.error(f"Error handling customer: {e}")
            raise
    
    def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        """Verify webhook signature from Stripe"""
        if not self.is_configured:
            return False
            
        try:
            stripe.Webhook.construct_event(
                payload, signature, self.webhook_secret
            )
            return True
        except ValueError:
            logger.error("Invalid payload")
            return False
        except stripe.error.SignatureVerificationError:
            logger.error("Invalid signature")
            return False
    
    def handle_successful_payment(self, session_data) -> Dict[str, Any]:
        """Handle successful payment and return subscription info"""
        try:
            # Extract metadata - handle both dict and Stripe object
            logger.info(f"Session data type: {type(session_data)}")
            logger.info(f"Session data attributes: {dir(session_data)}")
            
            if hasattr(session_data, 'metadata'):
                metadata = session_data.metadata
                logger.info(f"Metadata from attribute: {metadata}")
            else:
                metadata = session_data.get("metadata", {})
                logger.info(f"Metadata from get(): {metadata}")
            
            # Get telegram_id from metadata
            logger.info(f"Metadata type: {type(metadata)}")
            if hasattr(metadata, 'get'):
                telegram_id = metadata.get("telegram_id")
                logger.info(f"Telegram ID from get(): {telegram_id}")
            else:
                telegram_id = getattr(metadata, 'telegram_id', None)
                logger.info(f"Telegram ID from getattr(): {telegram_id}")
            
            if not telegram_id:
                logger.error("No telegram_id in payment metadata")
                logger.error(f"Metadata: {metadata}")
                return None
            
            # Calculate subscription dates
            start_date = datetime.utcnow()
            expiry_date = start_date + timedelta(days=30)  # Adjust based on your subscription period
            
            # Get session data - handle both dict and Stripe object
            if hasattr(session_data, 'customer'):
                customer_id = session_data.customer
            else:
                customer_id = session_data.get("customer")
            
            if hasattr(session_data, 'id'):
                session_id = session_data.id
            else:
                session_id = session_data.get("id")
            
            if hasattr(session_data, 'amount_total'):
                amount_total = session_data.amount_total
            else:
                amount_total = session_data.get("amount_total", 0)
            
            if hasattr(session_data, 'currency'):
                currency = session_data.currency
            else:
                currency = session_data.get("currency", "usd")
            
            subscription_data = {
                "telegram_id": int(telegram_id),
                "stripe_customer_id": customer_id,
                "stripe_session_id": session_id,
                "status": "active",
                "subscription_type": "premium",  # Adjust based on your product
                "start_date": start_date,
                "expiry_date": expiry_date,
                "amount_paid": amount_total / 100,  # Convert from cents
                "currency": currency,
                "updated_at": datetime.utcnow()
            }
            
            logger.info(f"Payment processed for telegram user {telegram_id}")
            return subscription_data
            
        except Exception as e:
            logger.error(f"Error handling successful payment: {e}")
            logger.error(f"Session data type: {type(session_data)}")
            logger.error(f"Session data: {session_data}")
            return None 