import os
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any

from google.cloud import logging as cloud_logging
from google.cloud import secretmanager
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from firestore_service import FirestoreService
from gcp_stripe_service import GCPStripeService

# Setup Cloud Logging
def setup_cloud_logging():
    """Setup Google Cloud Logging"""
    try:
        cloud_logging_client = cloud_logging.Client()
        cloud_logging_client.setup_logging()
        logger.info("Cloud Logging configured")
    except Exception as e:
        logger.warning(f"Could not setup Cloud Logging: {e}")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Setup cloud logging if running in GCP
if os.getenv('GOOGLE_CLOUD_PROJECT'):
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
        
        # Get VIP chat ID from Secret Manager (optional)
        vip_chat_id_str = self._get_secret("vip-chat-id")
        self.vip_chat_id = int(vip_chat_id_str) if vip_chat_id_str else None
        
        if self.vip_chat_id:
            logger.info(f"VIP chat ID configured: {self.vip_chat_id}")
        else:
            logger.warning("VIP chat ID not configured. Group management features will be disabled.")
        
        # Initialize Telegram application
        self.application = None
        
    def _get_secret(self, secret_name: str) -> str:
        """Get secret from GCP Secret Manager"""
        try:
            client = secretmanager.SecretManagerServiceClient()
            name = f"projects/{self.project_id}/secrets/{secret_name}/versions/latest"
            response = client.access_secret_version(request={"name": name})
            return response.payload.data.decode("UTF-8")
        except Exception as e:
            logger.error(f"Error accessing secret {secret_name}: {e}")
            # Fallback to environment variables for development
            return os.getenv(secret_name.upper().replace('-', '_'))

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a message when the command /start is issued."""
        # Create keyboard with Subscribe button
        keyboard = [
            [InlineKeyboardButton("Subscribe", callback_data="subscribe")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
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
        
        await update.message.reply_text(
            f"Welcome to AMBetz, {first_name}! 🎮🏀🏒⚾\n\n"
            f"We provide premium betting tips and predictions for:\n"
            f"• Esports\n"
            f"• NBA\n"
            f"• NHL\n"
            f"• MLB\n"
            f"• UCL\n"
            f"• And much more!\n\n"
            f"Join our VIP group to get exclusive daily picks and maximize your winnings!",
            reply_markup=reply_markup
        )
        logger.info(f"User {username} (ID: {user_id}) started the bot")

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Check subscription status."""
        user_id = update.effective_user.id
        
        try:
            # Get subscription from Firestore
            subscription = self.firestore_service.get_subscription(user_id)
            
            if subscription:
                start_date = subscription.get("start_date")
                expiry_date = subscription.get("expiry_date")
                status = subscription.get("status", "unknown")
                subscription_type = subscription.get("subscription_type", "basic")
                
                # Format dates
                start_str = start_date.strftime("%Y-%m-%d %H:%M:%S") if start_date else "N/A"
                expiry_str = expiry_date.strftime("%Y-%m-%d %H:%M:%S") if expiry_date else "N/A"
                
                message = (
                    f"📊 *Subscription Status*\n\n"
                    f"Status: {status.upper()}\n"
                    f"Type: {subscription_type}\n"
                    f"Start Date: {start_str}\n"
                    f"Expiry Date: {expiry_str}\n"
                )
                
                # Create renewal button if expired
                if status == "expired":
                    keyboard = [[InlineKeyboardButton("Renew Subscription", callback_data="subscribe")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode="Markdown")
                else:
                    await update.message.reply_text(message, parse_mode="Markdown")
            else:
                # No subscription found
                keyboard = [[InlineKeyboardButton("Subscribe Now", callback_data="subscribe")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                    "You don't have an active subscription. Subscribe now to get started!",
                    reply_markup=reply_markup
                )
        except Exception as e:
            logger.error(f"Error in status_command: {e}")
            await update.message.reply_text(f"❌ Error checking subscription status: {e}")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a message when the command /help is issued."""
        help_text = """
🎮🏀🏒⚾ *AMBetz VIP Betting Tips*

*Available Commands:*
/start - Welcome message and subscription options
/status - Check your current subscription status
/help - Show this help message

*How to Subscribe:*
1. Use /start to see subscription options
2. Click "Subscribe" to begin payment process
3. Complete payment securely through Stripe
4. Get instant access to VIP group!

*Need Help?*
Contact support if you have any questions about your subscription.
"""
        await update.message.reply_text(help_text, parse_mode="Markdown")

    async def test_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Create a test subscription for the user (development purposes)."""
        user_id = update.effective_user.id
        start_date = datetime.utcnow()
        expiry_date = start_date + timedelta(days=30)  # 30-day subscription
        
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

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle button callbacks."""
        query = update.callback_query
        await query.answer()
        
        if query.data == "subscribe":
            # Check if Stripe is configured
            if self.stripe_service.is_configured:
                try:
                    # Get user information
                    user_id = update.effective_user.id
                    username = update.effective_user.username
                    
                    # Create payment link
                    payment_url = self.stripe_service.create_payment_link(user_id, username)
                    
                    # Send payment link to user
                    keyboard = [
                        [InlineKeyboardButton("💳 Pay Now", url=payment_url)],
                        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await query.message.reply_text(
                        "🎉 Ready to subscribe!\n\n"
                        "Click the button below to complete your payment securely through Stripe.\n\n"
                        "✅ Secure payment processing\n"
                        "✅ Instant activation\n"
                        "✅ 30-day subscription",
                        reply_markup=reply_markup
                    )
                    
                except Exception as e:
                    logger.error(f"Error creating payment link: {e}")
                    await query.message.reply_text(
                        "❌ Sorry, there was an error processing your request. Please try again later."
                    )
            else:
                # Stripe not configured - show message like original bot
                await query.message.reply_text(
                    "This is the subscription flow. In production, this would connect to a payment provider.\n\n"
                    "For testing, you can use /test to create a test subscription."
                )
        
        elif query.data == "cancel":
            await query.message.reply_text("❌ Subscription cancelled. You can subscribe anytime using /start")

    async def check_expired_subscriptions(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Check for expired subscriptions and take action."""
        if not self.vip_chat_id:
            logger.warning("VIP_CHAT_ID not set. Cannot remove users from group.")
            
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
                if self.vip_chat_id:
                    try:
                        user_info = self.firestore_service.get_user(telegram_id)
                        username = user_info.get("username", "Unknown") if user_info else "Unknown"
                        
                        # Ban the user from the group for a short time (this effectively removes them)
                        await context.bot.ban_chat_member(
                            chat_id=self.vip_chat_id,
                            user_id=telegram_id,
                            until_date=datetime.now() + timedelta(seconds=35)  # Minimum time
                        )
                        
                        logger.info(f"Removed user {username} (ID: {telegram_id}) from VIP group")
                        
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
                        logger.error(f"Failed to remove user {telegram_id} from VIP group: {e}")
        except Exception as e:
            logger.error(f"Error in check_expired_subscriptions: {e}")

    def setup_application(self) -> Application:
        """Setup and configure the Telegram application"""
        logger.info("Setting up Telegram bot application...")
        
        # Create the Application
        self.application = Application.builder().token(self.bot_token).build()
        
        # Add command handlers
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        
        # Add development commands only if in development mode
        is_development = os.getenv('DEVELOPMENT_MODE', 'false').lower() == 'true'
        if is_development:
            self.application.add_handler(CommandHandler("test", self.test_command))
            self.application.add_handler(CommandHandler("expire", self.expire_command))
            logger.info("Development commands (/test, /expire) enabled")
        else:
            logger.info("Production mode: Development commands disabled")
        
        self.application.add_handler(CallbackQueryHandler(self.button_callback))
        
        # Set up job to check for expired subscriptions (every 1 hour)
        job_queue = self.application.job_queue
        if job_queue:
            job_queue.run_repeating(self.check_expired_subscriptions, interval=3600, first=10)
            logger.info("Set up job to check for expired subscriptions every hour")
        else:
            logger.warning("Job queue not available. Expired subscription checking will be disabled.")
        
        return self.application

    async def run_polling(self):
        """Run the bot in polling mode"""
        if not self.application:
            self.setup_application()
        
        logger.info("Starting bot in polling mode...")
        await self.application.run_polling(allowed_updates=["message", "callback_query"])

def main():
    """Main function to run the bot"""
    try:
        # Initialize bot
        bot = GCPTelegramBot()
        
        # Setup application
        bot.setup_application()
        
        # Run the bot
        asyncio.run(bot.run_polling())
        
    except Exception as e:
        logger.error(f"Error starting bot: {e}")
        raise

if __name__ == "__main__":
    main() 