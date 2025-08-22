# AMBetz VIP Telegram Bot
import os
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any

from google.cloud import logging as cloud_logging
from google.cloud import secretmanager
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Chat
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

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
        
        # Get VIP chat IDs from Secret Manager (optional)
        vip_announcements_id_str = self._get_secret("vip-announcements-id")
        vip_chat_id_str = self._get_secret("vip-chat-id")  # Use existing vip-chat-id for discussion
        
        self.vip_announcements_id = int(vip_announcements_id_str) if vip_announcements_id_str else None
        self.vip_discussion_id = int(vip_chat_id_str) if vip_chat_id_str else None
        
        # For backward compatibility, if no announcements ID is set, use vip-chat-id for announcements too
        if not self.vip_announcements_id and vip_chat_id_str:
            self.vip_announcements_id = int(vip_chat_id_str)
            logger.info(f"Using vip-chat-id for both announcements and discussion: {self.vip_announcements_id}")
        
        # Get admin Telegram ID for notifications (optional)
        admin_id_str = self._get_secret("admin-telegram-id")
        self.admin_telegram_id = int(admin_id_str) if admin_id_str else None
        
        if self.vip_announcements_id:
            logger.info(f"VIP announcements chat ID configured: {self.vip_announcements_id}")
        if self.vip_discussion_id:
            logger.info(f"VIP discussion chat ID configured: {self.vip_discussion_id}")
        if self.admin_telegram_id:
            logger.info(f"Admin Telegram ID configured: {self.admin_telegram_id}")
        
        if not self.vip_announcements_id and not self.vip_discussion_id:
            logger.warning("No VIP chat IDs configured. Group management features will be disabled.")
        
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

    def _is_private_chat(self, update: Update) -> bool:
        """Check if the current chat is a private chat (PM)"""
        if not update.effective_chat:
            return False
        return update.effective_chat.type == Chat.PRIVATE

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a message when the command /start is issued."""
        # Only respond in private chats
        if not self._is_private_chat(update):
            logger.info(f"Ignoring /start command in group chat {update.effective_chat.id}")
            return
        
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
        """Send notification to admin when a user is kicked from VIP group"""
        if not self.admin_telegram_id:
            logger.warning("No admin Telegram ID configured. Skipping admin notification.")
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
            
            await self.application.bot.send_message(
                chat_id=self.admin_telegram_id,
                text=message,
                parse_mode="Markdown"
            )
            
            logger.info(f"Admin notification sent for kicked user {user_id}")
            
        except Exception as e:
            logger.error(f"Failed to send admin notification for user {user_id}: {e}")

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a message when the command /help is issued."""
        # Only respond in private chats
        if not self._is_private_chat(update):
            logger.info(f"Ignoring /help command in group chat {update.effective_chat.id}")
            return
        
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
4. Receive exclusive one-time invite links for VIP groups!

*VIP Groups:*
• **Announcements Channel**: Daily picks and betting tips (one-time invite link)
• **Discussion Group**: Chat with other VIP members (one-time invite link)

*Need Help?*
Contact AM if you have any questions about your subscription.
"""
        await update.message.reply_text(help_text, parse_mode="Markdown")

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

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle button callbacks."""
        # Only respond in private chats
        if not self._is_private_chat(update):
            logger.info(f"Ignoring button callback in group chat {update.effective_chat.id}")
            return
        
        query = update.callback_query
        await query.answer()
        
        if query.data == "subscribe":
            # Check if user already has an active subscription
            user_id = update.effective_user.id
            existing_subscription = self.firestore_service.get_subscription(user_id)
            if existing_subscription and existing_subscription.get('status') == 'active':
                expiry_date = existing_subscription['expiry_date']
                await query.message.reply_text(
                    f"❌ **Subscription Already Active**\n\n"
                    f"You already have an active subscription that expires on:\n"
                    f"**{expiry_date.strftime('%Y-%m-%d %H:%M:%S')}**\n\n"
                    f"You cannot subscribe again until your current subscription expires.\n\n"
                    f"Use `/status` to check your current subscription."
                )
                return
            
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
        
        await update.message.reply_text(info_message, parse_mode="Markdown")
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
                logger.error(f"Failed to generate invite link for announcements group: {e}")
        
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
                logger.error(f"Failed to generate invite link for discussion group: {e}")
        
        return invite_links

    async def send_vip_invite_links(self, user_id: int, invite_links: Dict[str, str], username: str = None):
        """Send VIP invite links to the user"""
        try:
            message = "🎉 *Welcome to AMBetz VIP!* 🎉\n\n"
            message += "Your subscription is now active! Here are your exclusive invite links:\n\n"
            
            if 'announcements' in invite_links:
                message += "📢 *VIP Announcements Channel*\n"
                message += "Get daily picks and betting tips:\n"
                message += f"👉 {invite_links['announcements']}\n\n"
            
            if 'discussion' in invite_links:
                message += "💬 *VIP Discussion Group*\n"
                message += "Chat with other VIP members:\n"
                message += f"👉 {invite_links['discussion']}\n\n"
            
            message += "⚠️ *Important:*\n"
            message += "• These links are *one-time use only*\n"
            message += "• They expire in *24 hours*\n"
            message += "• *Do not share* these links with others\n"
            message += "• Use them immediately to join the VIP groups\n\n"
            
            message += "🎯 *Next Steps:*\n"
            message += "1. Click the links above to join both groups\n"
            message += "2. Start receiving daily VIP picks\n"
            message += "3. Connect with other VIP members\n\n"
            
            message += "Use `/status` to check your subscription anytime!"
            
            await self.application.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode="Markdown"
            )
            
            logger.info(f"Sent VIP invite links to user {username} (ID: {user_id})")
            
        except Exception as e:
            logger.error(f"Failed to send VIP invite links to user {user_id}: {e}")
            # Fallback message without Markdown
            try:
                await self.application.bot.send_message(
                    chat_id=user_id,
                    text="🎉 Welcome to AMBetz VIP! Your subscription is active. Please contact AM for your invite links."
                )
            except Exception as fallback_error:
                logger.error(f"Failed to send fallback message: {fallback_error}")

    def setup_application(self) -> Application:
        """Setup and configure the Telegram application"""
        logger.info("Setting up Telegram bot application...")
        
        # Create the Application
        self.application = Application.builder().token(self.bot_token).build()
        
        # Add command handlers
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("chatinfo", self.get_chat_info))
        
        # Add development commands only if in development mode
        is_development = os.getenv('DEVELOPMENT_MODE', 'false').lower() == 'true'
        if is_development:
            self.application.add_handler(CommandHandler("test", self.test_command))
            self.application.add_handler(CommandHandler("expire", self.expire_command))
            logger.info("Development commands (/test, /expire) enabled")
        else:
            logger.info("Production mode: Development commands disabled")
        
        self.application.add_handler(CallbackQueryHandler(self.button_callback))
        self.application.add_handler(MessageHandler(filters.ALL, self.handle_message))
        
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