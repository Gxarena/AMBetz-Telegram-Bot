import os
import logging
import datetime
import asyncio
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from pymongo import MongoClient
from pymongo.server_api import ServerApi

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Get bot token from environment variable
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    # For testing, you should set this to your actual token
    print("\n" + "="*80)
    print("ATTENTION: Please replace TOKEN_PLACEHOLDER below with your actual bot token")
    print("or set the TELEGRAM_BOT_TOKEN environment variable.")
    print("="*80 + "\n")
    # Replace this with your actual token if testing
    BOT_TOKEN = "7244340791:AAEaJyGtIbL7K8vIyLNDiHvSF25ewvS3Y-U"
    logger.warning("Using placeholder token. Please set your real token.")

# Get VIP group chat ID
VIP_CHAT_ID = os.getenv("VIP_CHAT_ID")
if VIP_CHAT_ID:
    try:
        VIP_CHAT_ID = int(VIP_CHAT_ID)
    except ValueError:
        logger.error(f"Invalid VIP_CHAT_ID: {VIP_CHAT_ID}. Must be a number.")
        VIP_CHAT_ID = None
else:
    logger.warning("VIP_CHAT_ID not set. Group management features will be disabled.")

# Database connection
db_available = True  # Set this to True to enable MongoDB features
try:
    if db_available:  # Only try to connect if we want database features
        MONGO_URI = os.getenv("MONGODB_URI")
        client = MongoClient(MONGO_URI, server_api=ServerApi('1'))

        try:
            client.admin.command('ping')
            print("Pinged your deployment. You successfully connected to MongoDB!")
        except Exception as e:
            print(e)

        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)  # 5 sec timeout
        db = client["Cluster0"]
        # Test connection
        client.server_info()
        logger.info(f"Connected to MongoDB at {MONGO_URI}, database: Cluster0")
        
        # Create indexes
        db.users.create_index("chat_id", unique=True)
        db.subscriptions.create_index("telegram_id", unique=True)
        db.subscriptions.create_index("expiry_date")
    else:
        logger.info("Database features are disabled for testing.")
except Exception as e:
    logger.warning(f"MongoDB connection failed: {e}")
    logger.warning("Running without database. Some features will be limited.")
    db_available = False

# Command handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    
    # Store user in database if available
    if db_available:
        try:
            db.users.update_one(
                {"chat_id": user_id},
                {"$set": {
                    "username": username,
                    "first_name": first_name,
                    "last_name": last_name,
                    "last_activity": datetime.datetime.utcnow()
                }},
                upsert=True
            )
        except Exception as e:
            logger.error(f"Database error storing user: {e}")
    
    await update.message.reply_text(
        f"Welcome {first_name}! I'm your subscription bot.",
        reply_markup=reply_markup
    )
    logger.info(f"User {username} (ID: {user_id}) started the bot")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check subscription status."""
    user_id = update.effective_user.id
    
    if not db_available:
        await update.message.reply_text(
            "⚠️ Database features are currently disabled.\n\n"
            "To enable subscription features, you need to:\n"
            "1. Install MongoDB or use MongoDB Atlas\n"
            "2. Set db_available = True in the code\n"
            "3. Update MONGO_URI in your .env file if needed\n"
            "4. Restart the bot"
        )
        return
    
    try:
        # Get subscription from database
        subscription = db.subscriptions.find_one({"telegram_id": user_id})
        
        if subscription:
            start_date = subscription.get("start_date").strftime("%Y-%m-%d %H:%M:%S") if subscription.get("start_date") else "N/A"
            expiry_date = subscription.get("expiry_date").strftime("%Y-%m-%d %H:%M:%S") if subscription.get("expiry_date") else "N/A"
            status = subscription.get("status", "unknown")
            subscription_type = subscription.get("subscription_type", "basic")
            
            message = (
                f"📊 *Subscription Status*\n\n"
                f"Status: {status.upper()}\n"
                f"Type: {subscription_type}\n"
                f"Start Date: {start_date}\n"
                f"Expiry Date: {expiry_date}\n"
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

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    help_text = """
*Available Commands:*
/start - Start the bot
/help - Show this help message
/status - Check your subscription status
/test - Create a test subscription (development only)
"""
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a test subscription for the user (development purposes)."""
    if not db_available:
        await update.message.reply_text(
            "⚠️ Database features are currently disabled.\n\n"
            "To enable test subscriptions, you need to:\n"
            "1. Install MongoDB or use MongoDB Atlas\n"
            "2. Set db_available = True in the code\n"
            "3. Update MONGO_URI in your .env file if needed\n"
            "4. Restart the bot"
        )
        return
        
    user_id = update.effective_user.id
    start_date = datetime.datetime.utcnow()
    expiry_date = start_date + datetime.timedelta(days=30)  # 30-day subscription
    
    try:
        db.subscriptions.update_one(
            {"telegram_id": user_id},
            {"$set": {
                "telegram_id": user_id,
                "start_date": start_date,
                "expiry_date": expiry_date,
                "subscription_type": "test",
                "status": "active",
                "updated_at": datetime.datetime.utcnow()
            }},
            upsert=True
        )
        await update.message.reply_text(
            f"✅ Test subscription created!\n\n"
            f"Start Date: {start_date.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Expiry Date: {expiry_date.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"Use /status to check your subscription."
        )
    except Exception as e:
        logger.error(f"Error creating test subscription: {e}")
        await update.message.reply_text(f"Failed to create test subscription: {e}")

# Callback query handler
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button callbacks."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "subscribe":
        await query.message.reply_text(
            "This is the subscription flow. In production, this would connect to a payment provider.\n\n"
            "For testing, you can use /test to create a test subscription."
        )

# Function to check for expired subscriptions
async def check_expired_subscriptions(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check for expired subscriptions and take action."""
    if not db_available:
        logger.warning("Database not available. Skipping expired subscription check.")
        return
        
    if not VIP_CHAT_ID:
        logger.warning("VIP_CHAT_ID not set. Cannot remove users from group.")
        
    logger.info("Checking for expired subscriptions...")
    
    # Current time in UTC
    current_time = datetime.datetime.utcnow()
    
    # Find expired but active subscriptions
    try:
        expired_subscriptions = list(db.subscriptions.find({
            "expiry_date": {"$lt": current_time},
            "status": "active"
        }))
        
        logger.info(f"Found {len(expired_subscriptions)} expired subscriptions")
        
        # Process each expired subscription
        for subscription in expired_subscriptions:
            telegram_id = subscription.get("telegram_id")
            if not telegram_id:
                continue
            
            # Update subscription status in database
            db.subscriptions.update_one(
                {"telegram_id": telegram_id},
                {"$set": {
                    "status": "expired",
                    "updated_at": current_time
                }}
            )
            
            logger.info(f"Marked subscription for user {telegram_id} as expired")
            
            # Try to remove user from the VIP group (if configured)
            if VIP_CHAT_ID:
                try:
                    user_info = db.users.find_one({"chat_id": telegram_id})
                    username = user_info.get("username", "Unknown") if user_info else "Unknown"
                    
                    # Ban the user from the group for a short time (this effectively removes them)
                    await context.bot.ban_chat_member(
                        chat_id=VIP_CHAT_ID,
                        user_id=telegram_id,
                        until_date=datetime.datetime.now() + datetime.timedelta(seconds=35)  # Minimum time
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

def main() -> None:
    """Initialize and run the bot."""
    # Validate token
    if BOT_TOKEN == "TOKEN_PLACEHOLDER":
        logger.error("Please set your bot token first!")
        return
    
    # Fix for Windows event loop issues
    if os.name == 'nt':  # Windows
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            logger.info("Set Windows-specific event loop policy")
        except Exception as e:
            logger.error(f"Failed to set Windows event loop policy: {e}")
    
    # Create the Application
    logger.info("Creating Telegram bot application...")
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("test", test_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Set up job to check for expired subscriptions (every 24 hours)
    job_queue = application.job_queue
    job_queue.run_repeating(check_expired_subscriptions, interval=86400, first=10)  # 86400 seconds = 24 hours
    logger.info("Set up job to check for expired subscriptions every 24 hours")
    
    # Start the Bot
    logger.info("Starting bot in polling mode...")
    application.run_polling(allowed_updates=["message", "callback_query"])
    
    logger.info("Bot has been stopped")

if __name__ == "__main__":
    main() 