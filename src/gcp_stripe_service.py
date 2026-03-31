import stripe
import os
import logging
import pytz
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from google.cloud import secretmanager

# Configure logging
logger = logging.getLogger(__name__)


def _subscription_period_bounds_unix(sub: Any) -> Tuple[Optional[int], Optional[int]]:
    """
    current_period_start/end on Subscription, or from first line item if Stripe omits top-level
    (common with current API shapes).
    """
    try:
        cs = getattr(sub, "current_period_start", None)
        ce = getattr(sub, "current_period_end", None)
        if cs is not None and ce is not None:
            return int(cs), int(ce)
    except (TypeError, ValueError):
        pass
    items = getattr(sub, "items", None)
    data = getattr(items, "data", None) if items is not None else None
    if not data:
        return None, None
    it0 = data[0]
    try:
        cs = getattr(it0, "current_period_start", None)
        ce = getattr(it0, "current_period_end", None)
        if cs is not None and ce is not None:
            return int(cs), int(ce)
    except (TypeError, ValueError, IndexError):
        pass
    return None, None


def _list_subscriptions_paginated(customer_id: str, status: str) -> List[Any]:
    """All subscriptions for customer with given status."""
    out: List[Any] = []
    params: Dict[str, Any] = {"customer": customer_id, "status": status, "limit": 100}
    while True:
        page = stripe.Subscription.list(**params)
        batch = page.data or []
        out.extend(batch)
        if not getattr(page, "has_more", False) or not batch:
            break
        params["starting_after"] = batch[-1].id
    return out

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

    def cancel_terminal_and_incomplete_subscriptions(self, customer_id: str) -> int:
        """
        Before starting a new Checkout session: remove subscriptions that are not 'active' or 'trialing'
        but still block a clean billing story (past_due, unpaid, incomplete). Prevents stacking a
        second subscription on the same customer while an old one is failed/abandoned.
        """
        cancelled = 0
        for status in ("past_due", "unpaid", "incomplete"):
            for sub in _list_subscriptions_paginated(customer_id, status):
                try:
                    stripe.Subscription.cancel(sub.id)
                    cancelled += 1
                    logger.info(
                        "Cancelled %s subscription %s before new checkout for customer %s",
                        status,
                        sub.id,
                        customer_id,
                    )
                except stripe.StripeError as exc:
                    logger.error(
                        "Failed to cancel %s subscription %s: %s", status, sub.id, exc
                    )
        return cancelled

    def cancel_other_subscriptions_except(self, customer_id: str, keep_subscription_id: str) -> int:
        """
        After a successful subscription Checkout: cancel every other subscription on this customer
        so only the newly paid subscription remains (VIP is single-product).
        """
        cancelled = 0
        for status in ("active", "trialing", "past_due", "unpaid", "incomplete"):
            for sub in _list_subscriptions_paginated(customer_id, status):
                if sub.id == keep_subscription_id:
                    continue
                try:
                    stripe.Subscription.cancel(sub.id)
                    cancelled += 1
                    logger.info(
                        "Cancelled extra subscription %s (status=%s) for customer %s; keeping %s",
                        sub.id,
                        status,
                        customer_id,
                        keep_subscription_id,
                    )
                except stripe.StripeError as exc:
                    logger.error("Failed to cancel subscription %s: %s", sub.id, exc)
        return cancelled

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
            self.cancel_terminal_and_incomplete_subscriptions(customer.id)

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
            self.cancel_terminal_and_incomplete_subscriptions(customer.id)

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
    
    def create_trial_subscription_checkout(self, telegram_id: int, telegram_username: str = None, trial_days: int = 3) -> str:
        """Create a Stripe checkout session for subscription with free trial period"""
        if not self.is_configured:
            raise ValueError("Stripe is not configured")
            
        try:
            # Sanitize username to remove problematic Unicode characters
            sanitized_username = self._sanitize_string(telegram_username) if telegram_username else ""
            
            # Create or retrieve customer
            customer = self.get_or_create_customer(telegram_id, sanitized_username)
            self.cancel_terminal_and_incomplete_subscriptions(customer.id)

            # Create checkout session for subscription with trial period
            checkout_session = stripe.checkout.Session.create(
                customer=customer.id,
                payment_method_types=['card'],
                line_items=[
                    {
                        'price': self.price_id,
                        'quantity': 1,
                    },
                ],
                mode='subscription',
                subscription_data={
                    'trial_period_days': trial_days,
                    'metadata': {
                        "telegram_id": str(telegram_id),
                        "telegram_username": sanitized_username,
                        "source": "gcp-bot",
                        "is_trial": "true"
                    }
                },
                success_url=f'https://t.me/AMBETZBot?start=success',
                cancel_url=f'https://t.me/AMBETZBot?start=cancelled',
                metadata={
                    "telegram_id": str(telegram_id),
                    "telegram_username": sanitized_username,
                    "source": "gcp-bot",
                    "is_trial": "true"
                }
            )
            
            logger.info(f"Trial subscription checkout session created for user {telegram_id} with {trial_days} day trial")
            return checkout_session.url
            
        except Exception as e:
            logger.error(f"Error creating trial subscription checkout: {e}")
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
                # Check if customer has active subscriptions (excluding trials)
                customer_id = customers.data[0].id
                active_subscriptions = stripe.Subscription.list(customer=customer_id, status='active')
                trialing_subscriptions = stripe.Subscription.list(customer=customer_id, status='trialing')
                
                # Check for active (non-trial) subscriptions that are not already ended
                # Allow resubscribe if all "active" subs have current_period_end in the past
                # (Stripe can still list them as active briefly before subscription.deleted fires)
                import time
                now_ts = int(time.time())
                truly_active = []
                for sub in (active_subscriptions.data or []):
                    _, period_end = _subscription_period_bounds_unix(sub)
                    if period_end is not None and period_end < now_ts:
                        # Period already ended - treat as over (Stripe may not have sent deleted yet)
                        continue
                    truly_active.append(sub)
                if truly_active:
                    # Customer has a real active subscription - block duplicate
                    logger.warning(f"Customer {customer_id} already has active subscription, rejecting new subscription attempt")
                    raise ValueError(f"Customer already has an active subscription. This should have been caught by the bot.")
                
                # Allow trialing subscriptions (user might be starting a new trial or converting trial to paid)
                # The bot logic will handle preventing duplicate trials
                if trialing_subscriptions.data:
                    logger.info(f"Customer {customer_id} has trialing subscription, allowing access")
                
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
    
    def cancel_active_subscriptions(self, telegram_id: int) -> bool:
        """Cancel all active and trialing subscriptions for a customer (dev/testing only)"""
        if not self.is_configured:
            raise ValueError("Stripe is not configured")
        
        try:
            # Find customer by telegram_id
            customers = stripe.Customer.search(
                query=f"metadata['telegram_id']:'{telegram_id}'"
            )
            
            if not customers.data:
                logger.info(f"No Stripe customer found for telegram_id {telegram_id}")
                return False
            
            customer_id = customers.data[0].id
            cancelled_count = 0
            
            # Cancel all active subscriptions
            active_subscriptions = stripe.Subscription.list(customer=customer_id, status='active')
            for sub in active_subscriptions.data:
                try:
                    stripe.Subscription.cancel(sub.id)
                    logger.info(f"Cancelled active subscription {sub.id} for customer {customer_id}")
                    cancelled_count += 1
                except Exception as e:
                    logger.error(f"Error cancelling subscription {sub.id}: {e}")
            
            # Cancel all trialing subscriptions
            trialing_subscriptions = stripe.Subscription.list(customer=customer_id, status='trialing')
            for sub in trialing_subscriptions.data:
                try:
                    stripe.Subscription.cancel(sub.id)
                    logger.info(f"Cancelled trialing subscription {sub.id} for customer {customer_id}")
                    cancelled_count += 1
                except Exception as e:
                    logger.error(f"Error cancelling trialing subscription {sub.id}: {e}")
            
            logger.info(f"Cancelled {cancelled_count} subscription(s) for telegram_id {telegram_id}")
            return cancelled_count > 0
            
        except Exception as e:
            logger.error(f"Error cancelling subscriptions for telegram_id {telegram_id}: {e}")
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
            
            # Try multiple ways to get metadata
            metadata = None
            if hasattr(session_data, 'metadata'):
                metadata = session_data.metadata
                logger.info(f"Metadata from attribute: {metadata}")
            elif hasattr(session_data, 'get'):
                metadata = session_data.get("metadata", {})
                logger.info(f"Metadata from get(): {metadata}")
            
            # Get telegram_id from metadata
            logger.info(f"Metadata type: {type(metadata)}")
            logger.info(f"Metadata content: {metadata}")
            
            telegram_id = None
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
                    logger.warning(f"Metadata is neither dict nor object with get/telegram_id: {type(metadata)}")
                    telegram_id = None
            except Exception as e:
                logger.error(f"Error accessing telegram_id from metadata: {e}")
                logger.error(f"Metadata type: {type(metadata)}")
                logger.error(f"Metadata content: {metadata}")
                telegram_id = None
            
            # FALLBACK: If no telegram_id in session metadata, try to get it from the customer
            if not telegram_id:
                logger.warning("No telegram_id in session metadata, attempting fallback methods...")
                
                # Get customer ID from session
                customer_id = None
                if hasattr(session_data, 'customer'):
                    customer_id = session_data.customer
                elif hasattr(session_data, 'get'):
                    customer_id = session_data.get("customer")
                
                if customer_id:
                    try:
                        # Retrieve customer from Stripe to get metadata
                        customer = stripe.Customer.retrieve(customer_id)
                        telegram_id = customer.metadata.get('telegram_id')
                        logger.info(f"Retrieved telegram_id from customer metadata: {telegram_id}")
                    except Exception as e:
                        logger.error(f"Error retrieving customer {customer_id}: {e}")
                
                # If still no telegram_id, try to find it by email in Firestore
                if not telegram_id and customer_id:
                    try:
                        customer = stripe.Customer.retrieve(customer_id)
                        customer_email = customer.email
                        if customer_email:
                            logger.info(f"Attempting to find telegram_id by email: {customer_email}")
                            
                            # Import FirestoreService here to avoid circular imports
                            from firestore_service import FirestoreService
                            project_id = os.getenv('GOOGLE_CLOUD_PROJECT')
                            firestore_service = FirestoreService(project_id)
                            
                            # Try to find user by email
                            user_data = firestore_service.get_user_by_email(customer_email)
                            if user_data and user_data.get('telegram_id'):
                                telegram_id = user_data['telegram_id']
                                logger.info(f"Found telegram_id by email lookup: {telegram_id}")
                                
                                # Update the Stripe customer with the found telegram_id
                                try:
                                    stripe.Customer.modify(
                                        customer_id,
                                        metadata={
                                            'telegram_id': str(telegram_id),
                                            'telegram_username': user_data.get('username', ''),
                                            'source': 'gcp-bot',
                                            'linked_by_email': 'true'
                                        }
                                    )
                                    logger.info(f"Updated Stripe customer {customer_id} with telegram_id {telegram_id}")
                                except Exception as e:
                                    logger.error(f"Failed to update Stripe customer metadata: {e}")
                            else:
                                logger.warning(f"Customer {customer_id} ({customer_email}) has no telegram_id - manual intervention required")
                    except Exception as e:
                        logger.error(f"Error getting customer email: {e}")
            
            if not telegram_id:
                logger.error("No telegram_id found in payment metadata or customer data")
                logger.error(f"Session metadata: {metadata}")
                logger.error("This payment cannot be processed - customer needs manual linking")
                return None

            # Get session data - handle both dict and Stripe object (needed before subscription cleanup)
            if hasattr(session_data, 'customer'):
                customer_id = session_data.customer
            else:
                customer_id = session_data.get("customer")

            subscription_object = None
            is_trial = False

            # For subscriptions, get the actual subscription period from Stripe
            if hasattr(session_data, 'subscription') and session_data.subscription:
                subscription_object = stripe.Subscription.retrieve(
                    session_data.subscription,
                    expand=["items.data"],
                )

                meta_is_trial = False
                if isinstance(metadata, dict):
                    meta_is_trial = metadata.get("is_trial") == "true"
                elif metadata is not None:
                    try:
                        meta_is_trial = metadata["is_trial"] == "true"
                    except Exception:
                        pass
                is_trial = (
                    subscription_object.status == "trialing" or meta_is_trial
                )

                cps, cpe = _subscription_period_bounds_unix(subscription_object)
                if cps is not None and cpe is not None:
                    start_date = datetime.fromtimestamp(cps, tz=pytz.UTC)
                    expiry_date = datetime.fromtimestamp(cpe, tz=pytz.UTC)
                elif is_trial and getattr(subscription_object, "trial_start", None) and getattr(
                    subscription_object, "trial_end", None
                ):
                    logger.warning(
                        "Subscription %s missing item period bounds, using trial_start/trial_end",
                        subscription_object.id,
                    )
                    start_date = datetime.fromtimestamp(
                        subscription_object.trial_start, tz=pytz.UTC
                    )
                    expiry_date = datetime.fromtimestamp(
                        subscription_object.trial_end, tz=pytz.UTC
                    )
                elif getattr(subscription_object, "created", None):
                    logger.warning(
                        "Subscription %s missing period bounds, using created + default window",
                        subscription_object.id,
                    )
                    start_date = datetime.fromtimestamp(
                        subscription_object.created, tz=pytz.UTC
                    )
                    expiry_date = start_date + timedelta(days=3 if is_trial else 30)
                else:
                    raise ValueError(
                        f"Subscription {subscription_object.id} has no date information available"
                    )

                if is_trial:
                    logger.info(
                        f"Trial subscription detected for user {telegram_id}, trial ends at {expiry_date}"
                    )
            else:
                # This is a one-time payment, calculate dates manually
                start_date = datetime.now(pytz.UTC)
                if os.getenv('DEVELOPMENT_MODE', 'false').lower() == 'true':
                    expiry_date = start_date + timedelta(minutes=1)
                else:
                    expiry_date = start_date + timedelta(days=30)
            
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
            
            # Determine subscription type and metadata
            subscription_type = "trial" if is_trial else "premium"
            metadata_dict = {}
            if is_trial:
                metadata_dict["is_trial"] = True
                metadata_dict["trial_started_at"] = datetime.utcnow().isoformat()
            
            subscription_data = {
                "telegram_id": int(telegram_id),
                "stripe_customer_id": customer_id,
                "stripe_session_id": session_id,
                "status": "active",
                "subscription_type": subscription_type,
                "start_date": start_date,
                "expiry_date": expiry_date,
                "amount_paid": amount_total / 100,  # Convert from cents (0 for trials)
                "currency": currency,
                "updated_at": datetime.utcnow(),
                "metadata": metadata_dict if metadata_dict else None,
            }

            if subscription_object is not None:
                subscription_data["stripe_subscription_id"] = subscription_object.id
                if customer_id:
                    removed = self.cancel_other_subscriptions_except(
                        str(customer_id), subscription_object.id
                    )
                    if removed:
                        logger.info(
                            "Post-checkout: cancelled %s other subscription(s) for customer %s",
                            removed,
                            customer_id,
                        )

            if is_trial:
                logger.info(f"Trial subscription processed for telegram user {telegram_id}, expires at {expiry_date}")
            else:
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