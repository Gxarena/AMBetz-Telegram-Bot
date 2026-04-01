# AMBetz VIP Telegram Bot
import os
from pathlib import Path

from dotenv import load_dotenv

# Load repo-root .env for local runs (`python src/gcp_bot.py` does not load it otherwise)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import logging
import asyncio
import pytz
from datetime import datetime, timedelta
from typing import Any, Dict, List

from google.cloud import logging as cloud_logging
from google.cloud import secretmanager
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Chat
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

import stripe
from stripe import StripeError

from firestore_service import FirestoreService
from gcp_stripe_service import ActiveSubscriptionExistsError, GCPStripeService

# Setup Cloud Logging (only on Cloud Run — locally use console logging; avoids wrong quota project from ADC)
def _running_on_cloud_run() -> bool:
    return bool(os.getenv("K_SERVICE"))

def setup_cloud_logging():
    """Ship logs to Cloud Logging (production). Uses GOOGLE_CLOUD_PROJECT explicitly."""
    try:
        project = os.getenv("GOOGLE_CLOUD_PROJECT")
        cloud_logging_client = cloud_logging.Client(project=project)
        cloud_logging_client.setup_logging()
        logger.info("Cloud Logging configured for project %s", project)
    except Exception as e:
        logger.warning(f"Could not setup Cloud Logging: {e}")

# Configure logging
# In development mode, use DEBUG level for more detailed logs
log_level = logging.DEBUG if os.getenv('DEVELOPMENT_MODE', 'false').lower() == 'true' else logging.INFO
logging.basicConfig(
    level=log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Local dev: skip Cloud Logging (ADC quota project may point at an old/deleted GCP project).
# Cloud Run sets K_SERVICE — enable Cloud Logging there only.
if os.getenv("GOOGLE_CLOUD_PROJECT") and _running_on_cloud_run():
    setup_cloud_logging()

class GCPTelegramBot:
    def __init__(self):
        """Initialize the GCP Telegram Bot"""
        self.project_id = os.getenv('GOOGLE_CLOUD_PROJECT')
        if not self.project_id:
            raise ValueError("GOOGLE_CLOUD_PROJECT environment variable is required")
        
        # Initialize services
        self.firestore_service = FirestoreService(self.project_id)
        self.stripe_service = GCPStripeService(self.project_id)
        
        # Get bot token from Secret Manager
        self.bot_token = self._get_secret("telegram-bot-token")
        if not self.bot_token:
            raise ValueError("Telegram bot token not found in Secret Manager")
        
        # Get VIP chat IDs from Secret Manager (optional)
        vip_announcements_id_str = self._get_secret("vip-announcements-id")
        vip_chat_id_str = self._get_secret("vip-chat-id")  # Use existing vip-chat-id for discussion
        
        self.vip_announcements_id = int(vip_announcements_id_str) if vip_announcements_id_str else None
        self.vip_discussion_id = int(vip_chat_id_str) if vip_chat_id_str else None
        
        # For backward compatibility, if no announcements ID is set, use vip-chat-id for announcements too
        if not self.vip_announcements_id and vip_chat_id_str:
            self.vip_announcements_id = int(vip_chat_id_str)
            logger.info(f"Using vip-chat-id for both announcements and discussion: {self.vip_announcements_id}")
        
        # Get admin Telegram IDs for notifications (optional)
        admin_ids_str = self._get_secret("admin-telegram-id")
        if admin_ids_str:
            # Parse comma-separated admin IDs
            try:
                self.admin_telegram_ids = [int(id_str.strip()) for id_str in admin_ids_str.split(',') if id_str.strip()]
                # For backward compatibility, keep the first admin ID as admin_telegram_id
                self.admin_telegram_id = self.admin_telegram_ids[0] if self.admin_telegram_ids else None
            except ValueError as e:
                logger.error(f"Error parsing admin IDs '{admin_ids_str}': {e}")
                self.admin_telegram_ids = []
                self.admin_telegram_id = None
        else:
            self.admin_telegram_ids = []
            self.admin_telegram_id = None
        
        if self.vip_announcements_id:
            logger.info(f"VIP announcements chat ID configured: {self.vip_announcements_id}")
        if self.vip_discussion_id:
            logger.info(f"VIP discussion chat ID configured: {self.vip_discussion_id}")
        if self.admin_telegram_ids:
            logger.info(f"Admin Telegram IDs configured: {self.admin_telegram_ids}")
        
        if not self.vip_announcements_id and not self.vip_discussion_id:
            logger.warning("No VIP chat IDs configured. Group management features will be disabled.")
        
        # Initialize Telegram application
        self.application = None
        
    def _get_secret(self, secret_name: str) -> str:
        """Get secret from GCP Secret Manager"""
        try:
            # Check if we're in test mode and use test secrets
            if os.getenv('DEVELOPMENT_MODE', 'false').lower() == 'true':
                secret_name = f"{secret_name}-test"
            
            client = secretmanager.SecretManagerServiceClient()
            name = f"projects/{self.project_id}/secrets/{secret_name}/versions/latest"
            response = client.access_secret_version(request={"name": name})
            return response.payload.data.decode("UTF-8")
        except Exception as e:
            logger.error(f"Error accessing secret {secret_name}: {e}")
            key = secret_name.upper().replace("-", "_")
            val = os.getenv(key)
            if val:
                return val
            if os.getenv("DEVELOPMENT_MODE", "false").lower() == "true" and secret_name.endswith(
                "-test"
            ):
                base = secret_name[: -len("-test")]
                val2 = os.getenv(base.upper().replace("-", "_"))
                if val2:
                    return val2
            # DEVELOPMENT_MODE=false expects TELEGRAM_BOT_TOKEN; many local .env files only set TELEGRAM_BOT_TOKEN_TEST
            if (
                not val
                and os.getenv("DEVELOPMENT_MODE", "false").lower() != "true"
                and secret_name == "telegram-bot-token"
            ):
                alt = os.getenv("TELEGRAM_BOT_TOKEN_TEST")
                if alt:
                    logger.warning(
                        "TELEGRAM_BOT_TOKEN is unset; using TELEGRAM_BOT_TOKEN_TEST. "
                        "Set TELEGRAM_BOT_TOKEN for production-style local runs."
                    )
                    return alt
            return val or ""

    def _is_private_chat(self, update: Update) -> bool:
        """Check if the current chat is a private chat (PM)"""
        if not update.effective_chat:
            return False
        return update.effective_chat.type == Chat.PRIVATE

    def build_main_menu_keyboard(self) -> InlineKeyboardMarkup:
        """Subscribe full width; other commands in two columns below."""
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⭐ Subscribe", callback_data="subscribe")],
                [
                    InlineKeyboardButton("📊 Status", callback_data="menu_status"),
                    InlineKeyboardButton("❓ Help", callback_data="menu_help"),
                ],
                [
                    InlineKeyboardButton("🚫 Cancel subscription", callback_data="menu_cancel"),
                    InlineKeyboardButton("ℹ️ Chat info", callback_data="menu_chatinfo"),
                ],
            ]
        )

    @staticmethod
    def _subscription_checkout_message_text() -> str:
        """Shared copy for checkout (single or multi-plan)."""
        return (
            "🎉 **Choose your plan** and pay securely through Stripe.\n\n"
            "Tap your billing period below. Each plan renews automatically until you cancel.\n\n"
            "✅ Secure payment processing\n"
            "✅ Instant activation\n"
            "✅ Recurring subscription (renews on your plan’s schedule)\n"
            "✅ Auto-renewal (cancel anytime)\n\n"
            "Subscription fees are non-refundable; cancel anytime to stop future charges."
        )

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a message when the command /start is issued."""
        # Only respond in private chats
        if not self._is_private_chat(update):
            logger.info(f"Ignoring /start command in group chat {update.effective_chat.id}")
            return
        
        # Get user information
        user_id = update.effective_user.id
        username = update.effective_user.username
        first_name = update.effective_user.first_name
        last_name = update.effective_user.last_name
        
        # Store user in Firestore
        user_data = {
            'chat_id': user_id,
            'username': username,
            'first_name': first_name,
            'last_name': last_name,
        }
        
        success = self.firestore_service.create_or_update_user(user_id, user_data)
        if not success:
            logger.error(f"Failed to store user data for {user_id}")
        
        # Check if user is eligible for free trial
        # subscription = self.firestore_service.get_subscription(user_id)
        # has_active_subscription = subscription and subscription.get('status') == 'active'
        # has_used_trial = self.firestore_service.has_used_trial(user_id)
        
        reply_markup = self.build_main_menu_keyboard()
        
        await update.message.reply_text(
            f"Welcome to AMBetz, {first_name}! 🎮🏀🏒⚾\n\n"
            f"We provide premium betting tips and predictions for:\n"
            f"• Esports\n"
            f"• NBA\n"
            f"• NHL\n"
            f"• MLB\n"
            f"• UCL\n"
            f"• And much more!\n\n"
            f"Join our VIP groups to get exclusive daily picks and maximize your winnings!",
            reply_markup=reply_markup
        )
        logger.info(f"User {username} (ID: {user_id}) started the bot")

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Check subscription status."""
        # Only respond in private chats
        if not self._is_private_chat(update):
            logger.info(f"Ignoring /status command in group chat {update.effective_chat.id}")
            return
        
        user_id = update.effective_user.id
        
        try:
            await self._send_status_reply(update, user_id)
        except Exception as e:
            logger.error(f"Error in status_command: {e}")
            await update.effective_message.reply_text(f"❌ Error checking subscription status: {e}")

    async def _send_status_reply(self, update: Update, user_id: int) -> None:
        """Send status text; works from /status and from menu button callbacks."""
        subscription = self.firestore_service.get_subscription(user_id)

        if subscription:
            start_date = subscription.get("start_date")
            expiry_date = subscription.get("expiry_date")
            status = subscription.get("status", "unknown")
            subscription_type = subscription.get("subscription_type", "basic")

            start_str = start_date.strftime("%Y-%m-%d %H:%M:%S") if start_date else "N/A"
            expiry_str = expiry_date.strftime("%Y-%m-%d %H:%M:%S") if expiry_date else "N/A"

            message = (
                f"📊 *Subscription Status*\n\n"
                f"Status: {status.upper()}\n"
                f"Type: {subscription_type}\n"
                f"Start Date: {start_str}\n"
                f"Expiry Date: {expiry_str}\n"
            )

            if status == "expired":
                keyboard = [[InlineKeyboardButton("Renew Subscription", callback_data="subscribe")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.effective_message.reply_text(
                    message, reply_markup=reply_markup, parse_mode="Markdown"
                )
            else:
                await update.effective_message.reply_text(message, parse_mode="Markdown")
        else:
            keyboard = [[InlineKeyboardButton("Subscribe Now", callback_data="subscribe")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.effective_message.reply_text(
                "You don't have an active subscription. Subscribe now to get started!",
                reply_markup=reply_markup,
            )

    async def add_user_to_vip_groups(self, user_id: int, username: str = None) -> bool:
        """Add user to both VIP groups (announcements and discussion)"""
        success = True
        
        if self.vip_announcements_id:
            try:
                await self.application.bot.unban_chat_member(
                    chat_id=self.vip_announcements_id,
                    user_id=user_id,
                    only_if_banned=True
                )
                logger.info(f"Added user {username} (ID: {user_id}) to VIP announcements group")
            except Exception as e:
                logger.error(f"Failed to add user {user_id} to VIP announcements group: {e}")
                success = False
        
        if self.vip_discussion_id:
            try:
                await self.application.bot.unban_chat_member(
                    chat_id=self.vip_discussion_id,
                    user_id=user_id,
                    only_if_banned=True
                )
                logger.info(f"Added user {username} (ID: {user_id}) to VIP discussion group")
            except Exception as e:
                logger.error(f"Failed to add user {user_id} to VIP discussion group: {e}")
                success = False
        
        return success

    async def notify_admin_user_kicked(self, user_id: int, username: str = None, reason: str = "subscription expired") -> None:
        """Send notification to all admins when a user is kicked from VIP group"""
        if not self.admin_telegram_ids:
            logger.warning("No admin Telegram IDs configured. Skipping admin notification.")
            return
        
        try:
            user_display = f"@{username}" if username else f"User ID: {user_id}"
            message = (
                f"🚫 **User Kicked from VIP Group**\n\n"
                f"**User:** {user_display}\n"
                f"**User ID:** `{user_id}`\n"
                f"**Reason:** {reason}\n"
                f"**Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
                f"⚠️ **Action Required:** Please manually remove this user from the VIP channel as well."
            )
            
            # Send to all admins
            for admin_id in self.admin_telegram_ids:
                try:
                    await self.application.bot.send_message(
                        chat_id=admin_id,
                        text=message,
                        parse_mode="Markdown"
                    )
                    logger.info(f"Admin notification sent to {admin_id} for kicked user {user_id}")
                except Exception as e:
                    logger.error(f"Failed to send notification to admin {admin_id}: {e}")
            
            logger.info(f"Admin notifications sent for kicked user {user_id}")
            
        except Exception as e:
            logger.error(f"Failed to send admin notification for user {user_id}: {e}")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a message when the command /help is issued."""
        # Only respond in private chats
        if not self._is_private_chat(update):
            logger.info(f"Ignoring /help command in group chat {update.effective_chat.id}")
            return

        await self._send_help_reply(update)

    async def _send_help_reply(self, update: Update) -> None:
        help_text = """
🎮🏀🏒⚾ *AMBetz VIP Betting Tips*

*Available Commands:*
/start - Welcome message and subscription options
/status - Check your current subscription status
/cancel - Cancel your subscription (keeps access until period end)
/help - Show this help message

*How to Subscribe:*
1. Use /start to see subscription options
2. Tap **Subscribe**, pick your billing period, then complete payment in Stripe
3. Receive exclusive one-time invite links for VIP groups!

*Subscription Management:*
• **Recurring billing** — Renews on your plan’s schedule until you cancel
• **Cancel anytime** — Use Cancel subscription or /cancel
• **Access until period end** — No immediate cutoff

*VIP Groups:*
• **Announcements Channel**: Daily picks and betting tips (one-time invite link)
• **Discussion Group**: Chat with other VIP members (one-time invite link)

*Need Help?*
Contact AM if you have any questions about your subscription.
"""
        await update.effective_message.reply_text(help_text, parse_mode="Markdown")

    async def test_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Create a test subscription for the user (development purposes)."""
        # Only respond in private chats
        if not self._is_private_chat(update):
            logger.info(f"Ignoring /test command in group chat {update.effective_chat.id}")
            return
        
        user_id = update.effective_user.id
        start_date = datetime.utcnow()
        # For testing: 1 minute subscription, for production: 30 days
        if os.getenv('DEVELOPMENT_MODE', 'false').lower() == 'true':
            expiry_date = start_date + timedelta(minutes=1)  # 1 minute for testing
        else:
            expiry_date = start_date + timedelta(days=30)  # 30 days for production
        
        try:
            success = self.firestore_service.upsert_subscription(
                telegram_id=user_id,
                start_date=start_date,
                expiry_date=expiry_date,
                subscription_type="test"
            )
            
            if success:
                await update.message.reply_text(
                    f"✅ Test subscription created!\n\n"
                    f"Start Date: {start_date.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Expiry Date: {expiry_date.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"Use /status to check your subscription."
                )
            else:
                await update.message.reply_text("❌ Failed to create test subscription.")
        except Exception as e:
            logger.error(f"Error creating test subscription: {e}")
            await update.message.reply_text(f"Failed to create test subscription: {e}")

    async def expire_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Expire the user's subscription immediately (testing purposes)."""
        # Only respond in private chats
        if not self._is_private_chat(update):
            logger.info(f"Ignoring /expire command in group chat {update.effective_chat.id}")
            return
        
        user_id = update.effective_user.id
        past_date = datetime.utcnow() - timedelta(days=1)  # 1 day ago
        
        try:
            # Get current subscription first
            current_sub = self.firestore_service.get_subscription(user_id)
            if not current_sub:
                await update.message.reply_text("❌ You don't have a subscription to expire.")
                return
            
            # Update expiry date to past but keep status as 'active' 
            # so the automated check can find it
            success = self.firestore_service.upsert_subscription(
                telegram_id=user_id,
                start_date=current_sub.get('start_date', past_date),
                expiry_date=past_date,
                subscription_type=current_sub.get('subscription_type', 'test')
            )
            
            if success:
                await update.message.reply_text(
                    f"⚠️ Subscription expiry date set to past for testing!\n\n"
                    f"Expiry Date: {past_date.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Status: ACTIVE (will be found by expired check)\n\n"
                    f"Now triggering expired check to test VIP group removal..."
                )
                
                # Trigger expired check manually for immediate testing
                await self.check_expired_subscriptions(context)
                
                await update.message.reply_text(
                    f"✅ Expired subscription check completed!\n\n"
                    f"Check logs to see if bot attempted to remove you from VIP group.\n"
                    f"(You're the group owner, so removal should fail gracefully)"
                )
                
            else:
                await update.message.reply_text("❌ Failed to set subscription expiry date.")
        except Exception as e:
            logger.error(f"Error expiring subscription: {e}")
            await update.message.reply_text(f"Failed to expire subscription: {e}")

    async def expired_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Check for expired subscriptions (admin command)."""
        # Only respond in private chats
        if not self._is_private_chat(update):
            logger.info(f"Ignoring /expired command in group chat {update.effective_chat.id}")
            return
        
        try:
            # Find expired subscriptions
            expired_subscriptions = self.firestore_service.find_expired_subscriptions()
            
            if not expired_subscriptions:
                await update.message.reply_text("✅ No expired subscriptions found.")
                return
            
            # Format the response
            message = f"📋 **Found {len(expired_subscriptions)} expired subscriptions:**\n\n"
            
            for i, sub in enumerate(expired_subscriptions, 1):
                user_id = sub.get('telegram_id', 'Unknown')
                expiry_date = sub.get('expiry_date', 'Unknown')
                status = sub.get('status', 'Unknown')
                
                # Format expiry date
                if hasattr(expiry_date, 'strftime'):
                    expiry_str = expiry_date.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    expiry_str = str(expiry_date)
                
                message += f"{i}. **User ID:** `{user_id}`\n"
                message += f"   **Status:** {status}\n"
                message += f"   **Expired:** {expiry_str}\n\n"
            
            await update.message.reply_text(message, parse_mode="Markdown")
            
        except Exception as e:
            logger.error(f"Error in expired_command: {e}")
            await update.message.reply_text(f"❌ Error checking expired subscriptions: {e}")

    async def resettrial_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Reset trial status for testing (development only)."""
        user_id = update.effective_user.id
        
        # Only respond in private chats
        if not self._is_private_chat(update):
            return
        
        # Only allow in development mode
        dev_mode = os.getenv('DEVELOPMENT_MODE', 'false').lower() == 'true'
        if not dev_mode:
            await update.message.reply_text(
                "❌ This command is only available in development mode."
            )
            return
        
        try:
            # First, cancel any active Stripe subscriptions
            try:
                self.stripe_service.cancel_active_subscriptions(user_id)
            except Exception:
                pass  # Subscription may not exist
            
            # Reset trial status in Firestore
            self.firestore_service.reset_trial_status(user_id)
            
            # Also expire any subscriptions in Firestore for clean testing
            subscription = self.firestore_service.get_subscription(user_id)
            if subscription:
                self.firestore_service.mark_subscription_expired(user_id)
                await update.message.reply_text(
                    f"✅ **Trial Status Reset for Testing**\n\n"
                    f"• Stripe subscription cancelled\n"
                    f"• Trial usage flag cleared\n"
                    f"• Firestore subscription expired\n\n"
                    f"Now you can test the free trial feature!\n\n"
                    f"Use `/start` to see the free trial button."
                )
            else:
                await update.message.reply_text(
                    f"✅ **Trial Status Reset for Testing**\n\n"
                    f"• Stripe subscription cancelled (if existed)\n"
                    f"• Trial usage flag cleared\n\n"
                    f"Now you can test the free trial feature!\n\n"
                    f"Use `/start` to see the free trial button."
                )
            
        except Exception as e:
            logger.error(f"Error resetting trial status for user {user_id}: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Error resetting trial status: {e}")

    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Cancel user's subscription."""
        # Only respond in private chats
        if not self._is_private_chat(update):
            logger.info(f"Ignoring /cancel command in group chat {update.effective_chat.id}")
            return
        
        user_id = update.effective_user.id
        
        try:
            # Check if user has an active subscription
            subscription = self.firestore_service.get_subscription(user_id)
            
            if not subscription or subscription.get('status') != 'active':
                await update.effective_message.reply_text(
                    "❌ **No Active Subscription Found**\n\n"
                    "You don't have an active subscription to cancel.\n\n"
                    "Use /start to subscribe if you'd like to join VIP!"
                )
                return
            
            # Check if subscription is already cancelled
            metadata = subscription.get('metadata', {})
            if metadata.get('cancelled'):
                expiry_date = subscription.get('expiry_date')
                await update.effective_message.reply_text(
                    f"⚠️ **Subscription Already Cancelled**\n\n"
                    f"Your subscription has already been cancelled and will expire on:\n"
                    f"**{expiry_date.strftime('%Y-%m-%d %H:%M:%S')}**\n\n"
                    f"You will continue to have VIP access until then."
                )
                return
            
            # Get Stripe customer ID
            stripe_customer_id = subscription.get('stripe_customer_id')
            if not stripe_customer_id:
                await update.effective_message.reply_text(
                    "❌ **Unable to Cancel Subscription**\n\n"
                    "We couldn't find your Stripe customer information. Please contact support for assistance."
                )
                return
            
            # Cancel the subscription in Stripe
            try:
                import stripe
                stripe.api_key = self.stripe_service.secret_key
                
                # Get active and trialing subscriptions for this customer (trial users can cancel too)
                subscriptions = stripe.Subscription.list(customer=stripe_customer_id, status='active')
                if not subscriptions.data:
                    subscriptions = stripe.Subscription.list(customer=stripe_customer_id, status='trialing')
                
                if not subscriptions.data:
                    await update.effective_message.reply_text(
                        "❌ **No Active Stripe Subscription Found**\n\n"
                        "We couldn't find an active subscription in Stripe. Please contact support."
                    )
                    return
                
                # Cancel the subscription
                stripe_subscription = subscriptions.data[0]
                stripe.Subscription.modify(
                    stripe_subscription.id,
                    cancel_at_period_end=True
                )
                
                # Update Firestore to mark as cancelled
                expiry_date = subscription.get('expiry_date')
                if expiry_date is None and stripe_subscription:
                    # Fallback from Stripe subscription object
                    end_ts = getattr(stripe_subscription, 'current_period_end', None)
                    if end_ts:
                        expiry_date = datetime.fromtimestamp(end_ts, tz=pytz.UTC)
                if expiry_date is not None and hasattr(expiry_date, 'strftime'):
                    expiry_str = expiry_date.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    expiry_str = 'end of billing period'
                success = self.firestore_service.upsert_subscription(
                    telegram_id=user_id,
                    start_date=subscription.get('start_date'),
                    expiry_date=expiry_date,
                    subscription_type=subscription.get('subscription_type', 'premium'),
                    stripe_customer_id=stripe_customer_id,
                    stripe_session_id=subscription.get('stripe_session_id'),
                    amount_paid=subscription.get('amount_paid'),
                    currency=subscription.get('currency'),
                    metadata={"cancelled": True, "cancelled_at": datetime.utcnow().isoformat()}
                )
                
                if success:
                    await update.effective_message.reply_text(
                        f"✅ **Subscription Cancelled Successfully**\n\n"
                        f"Your subscription has been cancelled and will expire on:\n"
                        f"**{expiry_str}**\n\n"
                        f"You will continue to have VIP access until then.\n\n"
                        f"Use /start to resubscribe when you're ready to return!"
                    )
                    logger.info(f"User {user_id} cancelled their subscription")
                else:
                    await update.effective_message.reply_text(
                        "❌ **Error Cancelling Subscription**\n\n"
                        "There was an error updating your subscription status. Please contact support."
                    )
                
            except Exception as e:
                logger.error(f"Error cancelling Stripe subscription for user {user_id}: {e}")
                await update.effective_message.reply_text(
                    "❌ **Error Cancelling Subscription**\n\n"
                    "There was an error cancelling your subscription. Please contact support for assistance."
                )
                
        except Exception as e:
            logger.error(f"Error in cancel_command: {e}")
            await update.effective_message.reply_text(f"❌ Error processing cancellation request: {e}")

    async def _reply_stripe_checkout_error(self, query, user_id: int, e: Exception) -> None:
        logger.error(
            "Error creating subscription checkout",
            extra={"telegram_id": user_id, "error": str(e)},
            exc_info=True,
        )
        if isinstance(e, ActiveSubscriptionExistsError):
            await query.message.reply_text(str(e))
            return
        dev = os.getenv("DEVELOPMENT_MODE", "false").lower() == "true"
        msg = "❌ Sorry, there was an error processing your request. Please try again later."
        if dev:
            stripe_detail = (
                (e.user_message or str(e)) if isinstance(e, StripeError) else str(e)
            )
            msg += f"\n\nDebug: {stripe_detail}"
            if isinstance(e, StripeError) and (
                getattr(e, "code", None) == "resource_missing"
                or "No such price" in str(e)
            ):
                msg += (
                    "\n\nLikely cause: **API key mode ≠ price mode**. "
                    "`sk_test_` keys only work with prices from Stripe **Test mode**; "
                    "**live** price IDs require `sk_live_` keys (and vice versa)."
                )
        await query.message.reply_text(msg)

    async def _reply_subscription_checkout(
        self,
        query,
        user_id: int,
        username: str | None,
        price_id: str | None,
    ) -> None:
        """Open Stripe checkout for the given price (or default monthly if price_id is None)."""
        existing_subscription = self.firestore_service.get_subscription(user_id)
        if existing_subscription and existing_subscription.get("status") == "active":
            expiry_date = existing_subscription["expiry_date"]
            await query.message.reply_text(
                f"❌ **Subscription Already Active**\n\n"
                f"You already have an active subscription that expires on:\n"
                f"**{expiry_date.strftime('%Y-%m-%d %H:%M:%S')}**\n\n"
                f"You cannot subscribe again until your current subscription expires.\n\n"
                f"Use `/status` to check your current subscription."
            )
            return

        if not self.stripe_service.is_configured:
            await query.message.reply_text(
                "This is the subscription flow. In production, this would connect to a payment provider.\n\n"
                "For testing, you can use /test to create a test subscription."
            )
            return

        try:
            payment_url = self.stripe_service.create_subscription_checkout(
                user_id, username, price_id=price_id
            )
            keyboard = [[InlineKeyboardButton("💳 Pay now", url=payment_url)]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.reply_text(
                self._subscription_checkout_message_text(),
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
        except Exception as e:
            await self._reply_stripe_checkout_error(query, user_id, e)

    async def _reply_subscription_checkouts_combined(
        self,
        query,
        user_id: int,
        username: str | None,
        plans: List[Dict[str, str]],
    ) -> None:
        """Single message: intro text + one Stripe Checkout URL button per plan."""
        existing_subscription = self.firestore_service.get_subscription(user_id)
        if existing_subscription and existing_subscription.get("status") == "active":
            expiry_date = existing_subscription["expiry_date"]
            await query.message.reply_text(
                f"❌ **Subscription Already Active**\n\n"
                f"You already have an active subscription that expires on:\n"
                f"**{expiry_date.strftime('%Y-%m-%d %H:%M:%S')}**\n\n"
                f"You cannot subscribe again until your current subscription expires.\n\n"
                f"Use `/status` to check your current subscription."
            )
            return

        if not self.stripe_service.is_configured:
            await query.message.reply_text(
                "This is the subscription flow. In production, this would connect to a payment provider.\n\n"
                "For testing, you can use /test to create a test subscription."
            )
            return

        rows: list = []
        first_error: Exception | None = None
        for p in plans:
            try:
                url = self.stripe_service.create_subscription_checkout(
                    user_id, username, price_id=p["price_id"]
                )
                rows.append([InlineKeyboardButton(f"💳 {p['label']}", url=url)])
            except Exception as e:
                if first_error is None:
                    first_error = e
                logger.error(
                    "Checkout session failed for plan %s",
                    p.get("key"),
                    exc_info=True,
                )

        if not rows:
            await self._reply_stripe_checkout_error(
                query, user_id, first_error or Exception("Checkout unavailable")
            )
            return

        await query.message.reply_text(
            self._subscription_checkout_message_text(),
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle button callbacks."""
        # Only respond in private chats
        if not self._is_private_chat(update):
            logger.info(f"Ignoring button callback in group chat {update.effective_chat.id}")
            return
        
        query = update.callback_query
        await query.answer()
        data = query.data

        if data == "menu_status":
            await self._send_status_reply(update, update.effective_user.id)
            return
        if data == "menu_help":
            await self._send_help_reply(update)
            return
        if data == "menu_cancel":
            await self.cancel_command(update, context)
            return
        if data == "menu_chatinfo":
            await self.get_chat_info(update, context)
            return

        if data.startswith("subscribe_plan:"):
            plan_key = data.split(":", 1)[1]
            price_id = self.stripe_service.price_id_for_plan_key(plan_key)
            if not price_id:
                await query.message.reply_text(
                    "That plan is not available. Please use /start and try Subscribe again."
                )
                return
            user_id = update.effective_user.id
            username = update.effective_user.username
            await self._reply_subscription_checkout(query, user_id, username, price_id)
            return

        if data == "subscribe":
            user_id = update.effective_user.id
            username = update.effective_user.username
            existing_subscription = self.firestore_service.get_subscription(user_id)
            if existing_subscription and existing_subscription.get("status") == "active":
                expiry_date = existing_subscription["expiry_date"]
                await query.message.reply_text(
                    f"❌ **Subscription Already Active**\n\n"
                    f"You already have an active subscription that expires on:\n"
                    f"**{expiry_date.strftime('%Y-%m-%d %H:%M:%S')}**\n\n"
                    f"You cannot subscribe again until your current subscription expires.\n\n"
                    f"Use `/status` to check your current subscription."
                )
                return

            plans = self.stripe_service.get_subscription_plan_options()
            if len(plans) > 1:
                await self._reply_subscription_checkouts_combined(
                    query, user_id, username, plans
                )
                return

            single_price = plans[0]["price_id"] if plans else None
            await self._reply_subscription_checkout(query, user_id, username, single_price)
            return
        
        if data == "free_trial":
            # Check if user already has an active subscription or trial
            user_id = update.effective_user.id
            username = update.effective_user.username or "Unknown"
            
            existing_subscription = self.firestore_service.get_subscription(user_id)
            
            # Check 1: Active subscription
            if existing_subscription and existing_subscription.get('status') == 'active':
                expiry_date = existing_subscription['expiry_date']
                await query.message.reply_text(
                    f"❌ **Already Have Active Access**\n\n"
                    f"You already have an active subscription that expires on:\n"
                    f"**{expiry_date.strftime('%Y-%m-%d %H:%M:%S')}**\n\n"
                    f"You cannot start a trial while you have an active subscription.\n\n"
                    f"Use `/status` to check your current subscription."
                )
                return
            
            # Check 2: Has used trial before
            has_used_trial = self.firestore_service.has_used_trial(user_id)
            if has_used_trial:
                await query.message.reply_text(
                    f"❌ **Free Trial Already Used**\n\n"
                    f"You have already used your free trial. Free trials are limited to one per user.\n\n"
                    f"Click the Subscribe button to get full VIP access with our monthly subscription!"
                )
                return
            
            # Check if Stripe is configured
            if self.stripe_service.is_configured:
                try:
                    # Create trial subscription checkout (3-day free trial)
                    trial_url = self.stripe_service.create_trial_subscription_checkout(user_id, username, trial_days=3)
                    
                    # Send trial link to user
                    keyboard = [
                        [InlineKeyboardButton("🆓 Start Free Trial", url=trial_url)],
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await query.message.reply_text(
                        "🆓 **Start Your 3-Day Free Trial!**\n\n"
                        "Click the button below to start your free trial. No credit card required until the trial ends!\n\n"
                        "✅ **3 days completely free**\n"
                        "✅ **Full VIP access** during trial\n"
                        "✅ **Cancel anytime** before trial ends\n"
                        "✅ **No charges** during trial period\n\n"
                        "After 3 days, your subscription will automatically continue at the regular price. "
                        "You can cancel anytime before the trial ends to avoid charges.\n\n"
                        "Subscription fees are non-refundable; cancel anytime to stop future charges.\n\n"
                        "⚠️ **Note:** You'll need to add a payment method to start the trial, but you won't be charged until after 3 days.",
                        reply_markup=reply_markup
                    )
                    
                except Exception as e:
                    logger.error(f"Error creating trial link for user {user_id}: {e}", exc_info=True)
                    await query.message.reply_text(
                        f"❌ Sorry, there was an error processing your trial request.\n\n"
                        f"Please try again later or contact support."
                    )
            else:
                # Stripe not configured - show message
                await query.message.reply_text(
                    "Trial subscriptions require Stripe to be configured. Please contact support."
                )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle all non-command messages. Only respond in private chats."""
        # # Only respond in private chats
        if not self._is_private_chat(update):
            chat_id = update.effective_chat.id
            chat_title = update.effective_chat.title or "Unknown Group"
            logger.info(f"Ignoring message in group chat '{chat_title}' (ID: {chat_id})")
            return
        
        # In private chats, we can optionally respond to regular messages
        # For now, we'll just log them but not respond
        user_id = update.effective_user.id
        username = update.effective_user.username
        first_name = update.effective_user.first_name
        
        # Handle different types of messages
        if update.message.text:
            message_content = f"Text: {update.message.text}"
        elif update.message.photo:
            message_content = f"Photo (caption: {update.message.caption or 'No caption'})"
        elif update.message.video:
            message_content = f"Video (caption: {update.message.caption or 'No caption'})"
        elif update.message.audio:
            message_content = f"Audio (caption: {update.message.caption or 'No caption'})"
        elif update.message.document:
            message_content = f"Document: {update.message.document.file_name or 'Unnamed file'}"
        elif update.message.sticker:
            message_content = f"Sticker: {update.message.sticker.emoji or 'No emoji'}"
        else:
            message_content = "Other message type"
        
        logger.info(f"Received message from user {first_name} (@{username}) (ID: {user_id}) in private chat: {message_content}")
        
        # Optionally, you could add a helpful response here
        # await update.message.reply_text("I only respond to commands. Use /help to see available commands.")

    async def handle_group_events(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle group events like new members joining or leaving."""
        chat_id = update.effective_chat.id
        chat_title = update.effective_chat.title or "Unknown"
        
        # Handle new members joining
        if update.message.new_chat_members:
            for member in update.message.new_chat_members:
                if not member.is_bot:  # Don't log bot joins
                    logger.info(f"New member {member.username or member.first_name} (ID: {member.id}) joined group '{chat_title}' (ID: {chat_id})")
        
        # Handle members leaving
        if update.message.left_chat_member:
            member = update.message.left_chat_member
            if not member.is_bot:  # Don't log bot leaves
                logger.info(f"Member {member.username or member.first_name} (ID: {member.id}) left group '{chat_title}' (ID: {chat_id})")
        
        # Note: We don't process regular group messages here to save costs
        # Only specific group events are processed

    async def get_chat_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Get chat information (temporary command for getting chat IDs)"""
        chat = update.effective_chat
        chat_id = chat.id
        chat_type = chat.type
        chat_title = chat.title or "Private Chat"
        
        info_message = (
            f"📝 **Chat Information**\n\n"
            f"**Chat ID:** `{chat_id}`\n"
            f"**Type:** {chat_type}\n"
            f"**Title:** {chat_title}\n"
        )
        
        if chat.username:
            info_message += f"**Username:** @{chat.username}\n"
        
        await update.effective_message.reply_text(info_message, parse_mode="Markdown")
        logger.info(f"Chat info requested for chat '{chat_title}' (ID: {chat_id})")

    async def check_expired_subscriptions(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Check for expired subscriptions and take action."""
        if not self.vip_announcements_id and not self.vip_discussion_id:
            logger.warning("No VIP chat IDs configured. Cannot remove users from groups.")
            return
        
        logger.info("Checking for expired subscriptions...")
        
        try:
            # Find expired subscriptions
            expired_subscriptions = self.firestore_service.find_expired_subscriptions()
            logger.info(f"Found {len(expired_subscriptions)} expired subscriptions")
            
            # Process each expired subscription
            for subscription in expired_subscriptions:
                telegram_id = subscription.get("telegram_id")
                if not telegram_id:
                    continue
                
                # Update subscription status in Firestore
                success = self.firestore_service.mark_subscription_expired(telegram_id)
                if not success:
                    logger.error(f"Failed to mark subscription expired for user {telegram_id}")
                    continue
                
                logger.info(f"Marked subscription for user {telegram_id} as expired")
                
                # Try to remove user from the VIP group (if configured)
                if self.vip_announcements_id:
                    try:
                        user_info = self.firestore_service.get_user(telegram_id)
                        username = user_info.get("username", "Unknown") if user_info else "Unknown"
                        
                        # Ban the user from the group for a short time (this effectively removes them)
                        await context.bot.ban_chat_member(
                            chat_id=self.vip_announcements_id,
                            user_id=telegram_id,
                            until_date=datetime.now() + timedelta(seconds=35)  # Minimum time
                        )
                        
                        logger.info(f"Removed user {username} (ID: {telegram_id}) from VIP announcements group")
                        
                        # Notify the admin about the kick
                        await self.notify_admin_user_kicked(telegram_id, username, "subscription expired")
                        
                        # Notify the user
                        try:
                            await context.bot.send_message(
                                chat_id=telegram_id,
                                text="⚠️ Your subscription has expired and you have been removed from the VIP group. "
                                    "Please renew your subscription to regain access."
                            )
                        except Exception as e:
                            logger.error(f"Could not notify user {telegram_id} about removal: {e}")
                    except Exception as e:
                        logger.error(f"Failed to remove user {telegram_id} from VIP announcements group: {e}")
                
                if self.vip_discussion_id:
                    try:
                        user_info = self.firestore_service.get_user(telegram_id)
                        username = user_info.get("username", "Unknown") if user_info else "Unknown"
                        
                        # Ban the user from the group for a short time (this effectively removes them)
                        await context.bot.ban_chat_member(
                            chat_id=self.vip_discussion_id,
                            user_id=telegram_id,
                            until_date=datetime.now() + timedelta(seconds=35)  # Minimum time
                        )
                        
                        logger.info(f"Removed user {username} (ID: {telegram_id}) from VIP discussion group")
                        
                        # Notify the user
                        try:
                            await context.bot.send_message(
                                chat_id=telegram_id,
                                text="⚠️ Your subscription has expired and you have been removed from the VIP group. "
                                    "Please renew your subscription to regain access."
                            )
                        except Exception as e:
                            logger.error(f"Could not notify user {telegram_id} about removal: {e}")
                    except Exception as e:
                        logger.error(f"Failed to remove user {telegram_id} from VIP discussion group: {e}")
        except Exception as e:
            logger.error(f"Error in check_expired_subscriptions: {e}")

    async def generate_one_time_invite_links(self, user_id: int, username: str = None) -> Dict[str, str]:
        """Generate one-time invite links for VIP channel and group"""
        invite_links = {}
        
        # Generate invite link for VIP announcements group (if configured)
        if self.vip_announcements_id:
            try:
                invite_link = await self.application.bot.create_chat_invite_link(
                    chat_id=self.vip_announcements_id,
                    name=f"VIP Access for {username or user_id}",
                    creates_join_request=False,
                    expire_date=datetime.utcnow() + timedelta(hours=24),  # Expire in 24 hours
                    member_limit=1  # One-time use
                )
                invite_links['announcements'] = invite_link.invite_link
                logger.info(f"Generated one-time invite link for announcements group for user {user_id}")
            except Exception as e:
                logger.error(
                    "VIP_INVITE_LINKS: create_chat_invite_link failed for announcements user_id=%s chat_id=%s: %s",
                    user_id,
                    self.vip_announcements_id,
                    e,
                    exc_info=True,
                )
        
        # Generate invite link for VIP discussion group (if configured)
        if self.vip_discussion_id:
            try:
                invite_link = await self.application.bot.create_chat_invite_link(
                    chat_id=self.vip_discussion_id,
                    name=f"VIP Access for {username or user_id}",
                    creates_join_request=False,
                    expire_date=datetime.utcnow() + timedelta(hours=24),  # Expire in 24 hours
                    member_limit=1  # One-time use
                )
                invite_links['discussion'] = invite_link.invite_link
                logger.info(f"Generated one-time invite link for discussion group for user {user_id}")
            except Exception as e:
                logger.error(
                    "VIP_INVITE_LINKS: create_chat_invite_link failed for discussion user_id=%s chat_id=%s: %s",
                    user_id,
                    self.vip_discussion_id,
                    e,
                    exc_info=True,
                )
        
        configured = []
        if self.vip_announcements_id:
            configured.append("announcements")
        if self.vip_discussion_id:
            configured.append("discussion")
        if not configured:
            logger.warning(
                "VIP_INVITE_LINKS: no VIP chat IDs configured (secrets vip-announcements-id / vip-chat-id); user_id=%s",
                user_id,
            )
        else:
            got = list(invite_links.keys())
            if not invite_links:
                logger.error(
                    "VIP_INVITE_LINKS: all invite generations failed user_id=%s username=%s expected=%s",
                    user_id,
                    username or "",
                    configured,
                )
            elif set(got) != set(configured):
                logger.error(
                    "VIP_INVITE_LINKS: partial failure user_id=%s username=%s expected=%s got=%s",
                    user_id,
                    username or "",
                    configured,
                    got,
                )
        
        return invite_links

    async def send_vip_invite_links(self, user_id: int, invite_links: Dict[str, str], username: str = None):
        """Send VIP invite links to the user. Plain text (no Markdown) so invite URLs with _ or * don't break parsing."""
        try:
            message = "🎉 Welcome to AMBetz VIP! 🎉\n\n"
            message += "Your subscription is now active! Here are your exclusive invite links:\n\n"
            
            if 'announcements' in invite_links:
                message += "📢 VIP Announcements Channel\n"
                message += "Get daily picks and betting tips:\n"
                message += f"👉 {invite_links['announcements']}\n\n"
            
            if 'discussion' in invite_links:
                message += "💬 VIP Discussion Group\n"
                message += "Chat with other VIP members:\n"
                message += f"👉 {invite_links['discussion']}\n\n"
            
            message += "⚠️ Important:\n"
            message += "• These links are one-time use only\n"
            message += "• They expire in 24 hours\n"
            message += "• Do not share these links with others\n"
            message += "• Use them immediately to join the VIP groups\n\n"
            
            message += "🎯 Next Steps:\n"
            message += "1. Click the links above to join both groups\n"
            message += "2. Start receiving daily VIP picks\n"
            message += "3. Connect with other VIP members\n\n"
            
            message += "Use /status to check your subscription anytime!"
            
            await self.application.bot.send_message(
                chat_id=user_id,
                text=message
            )
            
            logger.info(f"Sent VIP invite links to user {username} (ID: {user_id})")
            
        except Exception as e:
            logger.error(
                "VIP_INVITE_LINKS: send_message failed user_id=%s username=%s: %s",
                user_id,
                username or "",
                e,
                exc_info=True,
            )
            # Fallback message without Markdown
            try:
                await self.application.bot.send_message(
                    chat_id=user_id,
                    text="🎉 Welcome to AMBetz VIP! Your subscription is active. Please contact AM for your invite links."
                )
                logger.warning(
                    "VIP_INVITE_LINKS: sent contact-admin fallback after send failure user_id=%s",
                    user_id,
                )
            except Exception as fallback_error:
                logger.error(
                    "VIP_INVITE_LINKS: fallback send_message also failed user_id=%s: %s",
                    user_id,
                    fallback_error,
                    exc_info=True,
                )

    def setup_application(self) -> Application:
        """Setup and configure the Telegram application"""
        logger.info("Setting up Telegram bot application...")
        
        # Create the Application
        self.application = Application.builder().token(self.bot_token).build()
        
        # Add command handlers
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("cancel", self.cancel_command))
        self.application.add_handler(CommandHandler("chatinfo", self.get_chat_info))
        
        # Add development commands only if in development mode
        is_development = os.getenv('DEVELOPMENT_MODE', 'false').lower() == 'true'
        if is_development:
            self.application.add_handler(CommandHandler("test", self.test_command))
            self.application.add_handler(CommandHandler("expire", self.expire_command))
            self.application.add_handler(CommandHandler("expired", self.expired_command))
            self.application.add_handler(CommandHandler("resettrial", self.resettrial_command))
            logger.info("Development commands (/test, /expire, /expired, /resettrial) enabled")
        else:
            self.application.add_handler(CommandHandler("expired", self.expired_command))
            logger.info("Production mode: Development commands disabled, /expired enabled")
        
        self.application.add_handler(CallbackQueryHandler(self.button_callback))
        # Process private messages for user commands
        self.application.add_handler(MessageHandler(filters.ChatType.PRIVATE, self.handle_message))
        
        # Process group messages only for specific operations (new member events, etc.)
        # This allows group management while saving costs on regular messages
        self.application.add_handler(MessageHandler(
            filters.ChatType.GROUPS & (filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER),
            self.handle_group_events
        ))
        
        # Set up job to check for expired subscriptions
        job_queue = self.application.job_queue
        if job_queue:
            # For testing: check every minute, for production: daily at 9 AM UTC
            if os.getenv('DEVELOPMENT_MODE', 'false').lower() == 'true':
                # Run every minute for testing (60 seconds)
                job_queue.run_repeating(self.check_expired_subscriptions, interval=60, first=10)
                logger.info("Set up job to check for expired subscriptions every minute (development mode)")
            else:
                # Calculate seconds until 9 AM UTC tomorrow
                now = datetime.utcnow()
                tomorrow_9am = now.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
                seconds_until_9am = (tomorrow_9am - now).total_seconds()
                
                # Run daily at 9 AM UTC (86400 seconds = 24 hours)
                job_queue.run_repeating(self.check_expired_subscriptions, interval=86400, first=seconds_until_9am)
                logger.info(f"Set up job to check for expired subscriptions daily at 9 AM UTC (first run in {seconds_until_9am:.0f} seconds)")
        else:
            logger.warning("Job queue not available. Expired subscription checking will be disabled.")
        
        return self.application

def main():
    """Main function to run the bot"""
    try:
        # Initialize bot
        bot = GCPTelegramBot()
        
        # Setup application
        bot.setup_application()
        
        # Run the bot - run_polling is synchronous and handles its own event loop
        if bot.application:
            logger.warning(
                "Starting local POLLING. Telegram allows only webhook OR polling for this bot token — "
                "polling removes your registered webhook. If this token is used in production (Cloud Run), "
                "production will stop receiving updates until you call setWebhook again for your service URL. "
                "For local work, prefer DEVELOPMENT_MODE=true and a separate test bot token from @BotFather."
            )
            logger.info("Starting bot in polling mode...")
            bot.application.run_polling(
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=True  # Ignore old updates when restarting
            )
        else:
            logger.error("Bot application not initialized")
            raise RuntimeError("Bot application not initialized")
        
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Error starting bot: {e}")
        raise

if __name__ == "__main__":
    main() 