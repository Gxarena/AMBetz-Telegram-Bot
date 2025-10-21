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
            # Check if we're in test mode and use test secrets
            if os.getenv('DEVELOPMENT_MODE', 'false').lower() == 'true':
                secret_name = f"{secret_name}-test"
            
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

    def create_subscription_checkout(self, telegram_id: int, telegram_username: str = None) -> str:
        """Create a Stripe checkout session for recurring subscription"""
        if not self.is_configured:
            raise ValueError("Stripe is not configured")
            
        try:
            # Sanitize username to remove problematic Unicode characters
            sanitized_username = self._sanitize_string(telegram_username) if telegram_username else ""
            
            # Create or retrieve customer
            customer = self.get_or_create_customer(telegram_id, sanitized_username)
            
            # Create checkout session for subscription
            checkout_session = stripe.checkout.Session.create(
                customer=customer.id,
                payment_method_types=['card'],
                line_items=[
                    {
                        'price': self.price_id,
                        'quantity': 1,
                    },
                ],
                mode='subscription',  # This makes it recurring!
                success_url=f'https://t.me/AMBETZBot?start=success',
                cancel_url=f'https://t.me/AMBETZBot?start=cancelled',
                metadata={
                    "telegram_id": str(telegram_id),
                    "telegram_username": sanitized_username,
                    "source": "gcp-bot"
                }
            )
            
            logger.info(f"Subscription checkout session created for user {telegram_id}")
            return checkout_session.url
            
        except Exception as e:
            logger.error(f"Error creating subscription checkout: {e}")
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
                # Check if customer has active subscriptions
                customer_id = customers.data[0].id
                active_subscriptions = stripe.Subscription.list(customer=customer_id, status='active')
                
                if active_subscriptions.data:
                    # Customer already has active subscription - this should not happen
                    # The bot should have prevented this, but as a safety measure, raise an error
                    logger.warning(f"Customer {customer_id} already has active subscription, rejecting new subscription attempt")
                    raise ValueError(f"Customer already has an active subscription. This should have been caught by the bot.")
                
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
            logger.info(f"Metadata content: {metadata}")
            
            try:
                if isinstance(metadata, dict):
                    telegram_id = metadata.get("telegram_id")
                    logger.info(f"Telegram ID from dict.get(): {telegram_id}")
                elif hasattr(metadata, 'get'):
                    telegram_id = metadata.get("telegram_id")
                    logger.info(f"Telegram ID from object.get(): {telegram_id}")
                elif hasattr(metadata, 'telegram_id'):
                    telegram_id = getattr(metadata, 'telegram_id', None)
                    logger.info(f"Telegram ID from getattr(): {telegram_id}")
                else:
                    logger.error(f"Metadata is neither dict nor object with get/telegram_id: {type(metadata)}")
                    telegram_id = None
            except Exception as e:
                logger.error(f"Error accessing telegram_id from metadata: {e}")
                logger.error(f"Metadata type: {type(metadata)}")
                logger.error(f"Metadata content: {metadata}")
                telegram_id = None
            
            if not telegram_id:
                logger.error("No telegram_id in payment metadata")
                logger.error(f"Metadata: {metadata}")
                return None
            
            # For subscriptions, get the actual subscription period from Stripe
            if hasattr(session_data, 'subscription') and session_data.subscription:
                # This is a subscription checkout, get the subscription details
                subscription = stripe.Subscription.retrieve(session_data.subscription)
                start_date = datetime.fromtimestamp(subscription.current_period_start)
                expiry_date = datetime.fromtimestamp(subscription.current_period_end)
            else:
                # This is a one-time payment, calculate dates manually
                start_date = datetime.utcnow()
                # For testing: 1 minute subscription, for production: 30 days
                if os.getenv('DEVELOPMENT_MODE', 'false').lower() == 'true':
                    expiry_date = start_date + timedelta(minutes=1)  # 1 minute for testing
                else:
                    expiry_date = start_date + timedelta(days=30)  # 30 days for production
            
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
    
    def _sanitize_string(self, text: str) -> str:
        """Sanitize string to remove problematic Unicode characters"""
        if not text:
            return ""
        
        try:
            # Remove or replace problematic Unicode characters
            # U+2028: Line Separator, U+2029: Paragraph Separator, U+0000: Null
            problematic_chars = {
                '\u2028': ' ',  # Line Separator -> space
                '\u2029': ' ',  # Paragraph Separator -> space
                '\u0000': '',   # Null -> empty
                '\u0001': '',   # Start of Heading -> empty
                '\u0002': '',   # Start of Text -> empty
                '\u0003': '',   # End of Text -> empty
                '\u0004': '',   # End of Transmission -> empty
                '\u0005': '',   # Enquiry -> empty
                '\u0006': '',   # Acknowledge -> empty
                '\u0007': '',   # Bell -> empty
                '\u0008': '',   # Backspace -> empty
                '\u000B': '',   # Vertical Tab -> empty
                '\u000C': '',   # Form Feed -> empty
                '\u000E': '',   # Shift Out -> empty
                '\u000F': '',   # Shift In -> empty
            }
            
            sanitized = text
            for char, replacement in problematic_chars.items():
                sanitized = sanitized.replace(char, replacement)
            
            # Also remove any other control characters
            sanitized = ''.join(char for char in sanitized if ord(char) >= 32 or char in '\n\r\t')
            
            logger.info(f"Sanitized string: '{text}' -> '{sanitized}'")
            return sanitized
            
        except Exception as e:
            logger.error(f"Error sanitizing string: {e}")
            # Return a safe fallback
            return text[:50] if text else ""  # Limit length and remove any problematic chars 