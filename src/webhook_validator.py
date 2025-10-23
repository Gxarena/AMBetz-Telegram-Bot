"""
Webhook validation system to ensure subscriptions can only be processed through the bot
"""

import logging
from typing import Optional, Dict, Any
import stripe

logger = logging.getLogger(__name__)

class WebhookValidator:
    """Validates that webhooks are from legitimate bot-initiated subscriptions"""
    
    def __init__(self, stripe_service):
        self.stripe_service = stripe_service
    
    def validate_checkout_session(self, session) -> Dict[str, Any]:
        """
        Validate that a checkout session was created through the bot
        
        Returns:
            Dict with validation result and error details
        """
        try:
            # Check if session has required metadata
            if not session.metadata:
                return {
                    'valid': False,
                    'error': 'No metadata found in session',
                    'action': 'reject_payment'
                }
            
            # Check for required bot metadata
            required_fields = ['telegram_id', 'source']
            for field in required_fields:
                if field not in session.metadata:
                    return {
                        'valid': False,
                        'error': f'Missing required field: {field}',
                        'action': 'reject_payment'
                    }
            
            # Validate source is from bot
            if session.metadata.get('source') != 'gcp-bot':
                return {
                    'valid': False,
                    'error': f'Invalid source: {session.metadata.get("source")}',
                    'action': 'reject_payment'
                }
            
            # Validate telegram_id is numeric
            try:
                telegram_id = int(session.metadata.get('telegram_id'))
                if telegram_id <= 0:
                    return {
                        'valid': False,
                        'error': 'Invalid telegram_id: must be positive integer',
                        'action': 'reject_payment'
                    }
            except (ValueError, TypeError):
                return {
                    'valid': False,
                    'error': 'Invalid telegram_id: must be numeric',
                    'action': 'reject_payment'
                }
            
            # Validate customer was created through bot
            if session.customer:
                try:
                    customer = stripe.Customer.retrieve(session.customer)
                    if not customer.metadata.get('telegram_id'):
                        return {
                            'valid': False,
                            'error': 'Customer not created through bot (no telegram_id)',
                            'action': 'reject_payment'
                        }
                    
                    # Ensure customer telegram_id matches session telegram_id
                    if customer.metadata.get('telegram_id') != session.metadata.get('telegram_id'):
                        return {
                            'valid': False,
                            'error': 'Customer telegram_id mismatch',
                            'action': 'reject_payment'
                        }
                except Exception as e:
                    logger.error(f"Error validating customer: {e}")
                    # If customer doesn't exist or can't be retrieved, that's also a validation failure
                    return {
                        'valid': False,
                        'error': f'Customer validation failed: {e}',
                        'action': 'reject_payment'
                    }
            
            # All validations passed
            return {
                'valid': True,
                'telegram_id': int(session.metadata.get('telegram_id')),
                'source': session.metadata.get('source')
            }
            
        except Exception as e:
            logger.error(f"Error validating checkout session: {e}")
            return {
                'valid': False,
                'error': f'Validation error: {e}',
                'action': 'reject_payment'
            }
    
    def validate_subscription_webhook(self, subscription) -> Dict[str, Any]:
        """
        Validate that a subscription webhook is from a bot-created subscription
        
        Returns:
            Dict with validation result and error details
        """
        try:
            # Get customer
            customer = stripe.Customer.retrieve(subscription.customer)
            
            # Check if customer has telegram_id
            if not customer.metadata.get('telegram_id'):
                return {
                    'valid': False,
                    'error': 'Customer not created through bot (no telegram_id)',
                    'action': 'skip_processing'
                }
            
            # Validate telegram_id is numeric
            try:
                telegram_id = int(customer.metadata.get('telegram_id'))
                if telegram_id <= 0:
                    return {
                        'valid': False,
                        'error': 'Invalid telegram_id in customer metadata',
                        'action': 'skip_processing'
                    }
            except (ValueError, TypeError):
                return {
                    'valid': False,
                    'error': 'Invalid telegram_id in customer metadata',
                    'action': 'skip_processing'
                }
            
            return {
                'valid': True,
                'telegram_id': telegram_id,
                'customer_id': customer.id
            }
            
        except Exception as e:
            logger.error(f"Error validating subscription webhook: {e}")
            return {
                'valid': False,
                'error': f'Validation error: {e}',
                'action': 'skip_processing'
            }
    
    def log_validation_failure(self, session_id: str, error: str, action: str):
        """Log validation failures for monitoring"""
        logger.warning(f"Webhook validation failed for session {session_id}: {error} (action: {action})")
        
        # You could also send alerts to admins here
        # or store in a database for analysis
