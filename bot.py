import os
import logging
import datetime
from typing import Dict, Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from pymongo import MongoClient
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Get bot token from environment variable
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Please set TELEGRAM_BOT_TOKEN environment variable")

# Get webhook URL from environment variable
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
if not WEBHOOK_URL:
    logger.warning("WEBHOOK_URL environment variable not set. Using polling mode.")

# Database connection
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "telegram_bot_db")
client = MongoClient(MONGO_URI)
db = client[DB_NAME]

# Command handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    # Create keyboard with Subscribe button
    keyboard = [
        [InlineKeyboardButton("Subscribe", callback_data="subscribe")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Store user in database
    user_id = update.effective_user.id
    username = update.effective_user.username
    first_name = update.effective_user.first_name
    last_name = update.effective_user.last_name
    
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
    
    await update.message.reply_text(
        f"Welcome {first_name}! I'm your subscription bot.",
        reply_markup=reply_markup
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check subscription status."""
    user_id = update.effective_user.id
    
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

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    help_text = """
Available commands:
/start - Start the bot
/help - Show this help message
/status - Check your subscription status
"""
    await update.message.reply_text(help_text)

# Callback query handler
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button callbacks."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "subscribe":
        # Here you would implement your subscription flow
        # For now, just acknowledge the button press
        await query.message.reply_text("Subscription flow will be implemented soon!")

def init_bot() -> Application:
    """Initialize and configure the bot application
    
    Returns:
        Application: Configured bot application instance
    """
    # Create application with builder pattern
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )
    
    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    logger.info("Bot initialized with handlers")
    return application

# Setup bot with webhook (for webhook mode)
async def setup_bot():
    """Setup the bot with webhook"""
    application = init_bot()
    
    # Set up webhook if URL is provided
    if WEBHOOK_URL:
        webhook_url = f"{WEBHOOK_URL}/webhook"
        await application.bot.set_webhook(webhook_url)
        logger.info(f"Webhook set to {webhook_url}")
    else:
        logger.warning("No webhook URL provided, webhook not set")
    
    return application

# Process updates from webhook (for webhook mode)
async def process_update(update_data: Dict[str, Any]):
    """Process update from webhook"""
    application = init_bot()
    await application.process_update(
        Update.de_json(data=update_data, bot=application.bot)
    ) 