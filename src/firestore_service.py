import os
import logging
import pytz
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

# Configure logging
logger = logging.getLogger(__name__)

class FirestoreService:
    def __init__(self, project_id: str = None):
        """Initialize Firestore client"""
        self.project_id = project_id or os.getenv('GOOGLE_CLOUD_PROJECT')
        if not self.project_id:
            raise ValueError("GOOGLE_CLOUD_PROJECT environment variable is required")
        
        try:
            self.db = firestore.Client(project=self.project_id)
            logger.info(f"Connected to Firestore in project: {self.project_id}")
        except Exception as e:
            logger.error(f"Failed to connect to Firestore: {e}")
            raise

    # User operations
    def get_user(self, chat_id: int) -> Optional[Dict]:
        """Get user by chat_id"""
        try:
            doc_ref = self.db.collection('users').document(str(chat_id))
            doc = doc_ref.get()
            if doc.exists:
                return doc.to_dict()
            return None
        except Exception as e:
            logger.error(f"Error getting user {chat_id}: {e}")
            return None

    def create_or_update_user(self, chat_id: int, user_data: Dict) -> bool:
        """Create or update user data"""
        try:
            doc_ref = self.db.collection('users').document(str(chat_id))
            user_data['last_activity'] = datetime.utcnow()
            doc_ref.set(user_data, merge=True)
            logger.info(f"User {chat_id} data updated")
            return True
        except Exception as e:
            logger.error(f"Error updating user {chat_id}: {e}")
            return False

    # Subscription operations
    def upsert_subscription(self, telegram_id: int, start_date: datetime, expiry_date: datetime, 
                           subscription_type: str = "basic", 
                           metadata: Dict[str, Any] = None,
                           stripe_customer_id: str = None,
                           stripe_session_id: str = None,
                           stripe_subscription_id: str = None,
                           amount_paid: float = None,
                           currency: str = None) -> bool:
        """
        Insert or update a subscription for a user
        
        Args:
            telegram_id: User's Telegram ID
            start_date: Start date of subscription
            expiry_date: Expiry date of subscription
            subscription_type: Type of subscription (default: "basic")
            metadata: Additional metadata about the subscription
            stripe_customer_id: Stripe customer ID (optional)
            stripe_session_id: Stripe session ID (optional)
            stripe_subscription_id: Stripe subscription ID for recurring subscriptions (optional)
            amount_paid: Amount paid in dollars (optional)
            currency: Payment currency (optional)
            
        Returns:
            bool: True if operation was successful
        """
        try:
            doc_ref = self.db.collection('subscriptions').document(str(telegram_id))
            subscription_data = {
                'telegram_id': telegram_id,
                'start_date': start_date,
                'expiry_date': expiry_date,
                'subscription_type': subscription_type,
                'status': 'active',
                'updated_at': datetime.utcnow()
            }
            
            # Add Stripe fields if provided
            if stripe_customer_id:
                subscription_data['stripe_customer_id'] = stripe_customer_id
            if stripe_session_id:
                subscription_data['stripe_session_id'] = stripe_session_id
            if stripe_subscription_id:
                subscription_data['stripe_subscription_id'] = stripe_subscription_id
            if amount_paid is not None:
                subscription_data['amount_paid'] = amount_paid
            if currency:
                subscription_data['currency'] = currency
            
            # Add metadata if provided
            if metadata:
                subscription_data['metadata'] = metadata
                
            # Use merge=False to completely overwrite old subscriptions
            # This prevents issues where old expired subscription data lingers
            doc_ref.set(subscription_data, merge=False)
            logger.info(f"Subscription upserted for user {telegram_id} (overwrote any old subscription)")
            return True
        except Exception as e:
            logger.error(f"Error in upsert_subscription for user {telegram_id}: {e}")
            return False

    def get_subscription(self, telegram_id: int) -> Optional[Dict]:
        """
        Get a user's subscription
        
        Args:
            telegram_id: User's Telegram ID
            
        Returns:
            Optional[Dict]: Subscription data or None if not found
        """
        try:
            doc_ref = self.db.collection('subscriptions').document(str(telegram_id))
            doc = doc_ref.get()
            if doc.exists:
                return doc.to_dict()
            return None
        except Exception as e:
            logger.error(f"Error getting subscription for user {telegram_id}: {e}")
            return None

    def find_expired_subscriptions(self) -> List[Dict]:
        """
        Find all subscriptions that have expired based on current UTC time
        
        Returns:
            List[Dict]: List of expired subscriptions
        """
        try:
            # Use timezone-aware datetime for Firestore query compatibility
            current_time = datetime.now(pytz.UTC)
            
            # Find subscriptions where expiry_date is less than current time
            # and status is still "active"
            query = (self.db.collection('subscriptions')
                    .where(filter=FieldFilter('expiry_date', '<', current_time))
                    .where(filter=FieldFilter('status', '==', 'active')))
            
            expired = []
            for doc in query.stream():
                sub_data = doc.to_dict()
                sub_data['telegram_id'] = int(doc.id)  # Ensure telegram_id is available
                
                # CRITICAL: If subscription has a stripe_subscription_id (recurring), add a grace period
                # This prevents race conditions where Stripe renewal webhook fires just before expiry check
                # Give 5 minutes grace period for Firestore to update from webhook
                if sub_data.get('stripe_subscription_id'):
                    grace_period_minutes = 5
                    expiry_with_grace = sub_data['expiry_date'] + timedelta(minutes=grace_period_minutes)
                    if current_time < expiry_with_grace:
                        logger.info(f"Skipping user {sub_data['telegram_id']} - has recurring subscription and within {grace_period_minutes}min grace period")
                        continue
                    else:
                        logger.warning(f"User {sub_data['telegram_id']} has recurring subscription but expired even with grace period - may need manual check")
                
                expired.append(sub_data)
            
            logger.info(f"Found {len(expired)} expired subscriptions")
            return expired
        except Exception as e:
            logger.error(f"Error finding expired subscriptions: {e}")
            return []

    def mark_subscription_expired(self, telegram_id: int) -> bool:
        """
        Mark a subscription as expired
        
        Args:
            telegram_id: User's Telegram ID
            
        Returns:
            bool: True if operation was successful
        """
        try:
            doc_ref = self.db.collection('subscriptions').document(str(telegram_id))
            doc_ref.update({
                'status': 'expired',
                'updated_at': datetime.utcnow()
            })
            logger.info(f"Marked subscription for user {telegram_id} as expired")
            return True
        except Exception as e:
            logger.error(f"Error marking subscription expired for user {telegram_id}: {e}")
            return False

    def get_subscription_by_stripe_session(self, stripe_session_id: str) -> Optional[Dict]:
        """
        Get subscription by Stripe session ID
        
        Args:
            stripe_session_id: Stripe session ID
            
        Returns:
            Optional[Dict]: Subscription data or None if not found
        """
        try:
            query = (self.db.collection('subscriptions')
                    .where(filter=FieldFilter('stripe_session_id', '==', stripe_session_id))
                    .limit(1))
            
            docs = list(query.stream())
            if docs:
                sub_data = docs[0].to_dict()
                sub_data['telegram_id'] = int(docs[0].id)
                return sub_data
            return None
        except Exception as e:
            logger.error(f"Error getting subscription by Stripe session {stripe_session_id}: {e}")
            return None

    def get_subscription_by_stripe_customer(self, stripe_customer_id: str) -> Optional[Dict]:
        """
        Get subscription by Stripe customer ID
        
        Args:
            stripe_customer_id: Stripe customer ID
            
        Returns:
            Optional[Dict]: Subscription data or None if not found
        """
        try:
            query = (self.db.collection('subscriptions')
                    .where(filter=FieldFilter('stripe_customer_id', '==', stripe_customer_id))
                    .limit(1))
            
            docs = list(query.stream())
            if docs:
                sub_data = docs[0].to_dict()
                sub_data['telegram_id'] = int(docs[0].id)
                return sub_data
            return None
        except Exception as e:
            logger.error(f"Error getting subscription by Stripe customer {stripe_customer_id}: {e}")
            return None