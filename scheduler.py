import os
import logging
import datetime
from typing import Any
from pymongo import MongoClient
from telegram.ext import Application
from telegram.error import TelegramError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Get group chat ID from environment variable
VIP_CHAT_ID = os.getenv("VIP_CHAT_ID")
if not VIP_CHAT_ID:
    logger.warning("VIP_CHAT_ID environment variable not set. Unable to remove expired users from group.")
try:
    VIP_CHAT_ID = int(VIP_CHAT_ID)
except (ValueError, TypeError):
    logger.error(f"Invalid VIP_CHAT_ID: {VIP_CHAT_ID}. Must be a valid integer.")
    VIP_CHAT_ID = None

# Database connection
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "telegram_bot_db")
client = MongoClient(MONGO_URI)
db = client[DB_NAME]

# Initialize scheduler
scheduler = AsyncIOScheduler()

async def check_expired_subscriptions(context: Application) -> None:
    """
    Check for expired subscriptions and remove users from the VIP group
    
    Args:
        context: The application context containing the bot
    """
    if not VIP_CHAT_ID:
        logger.warning("VIP_CHAT_ID not set. Skipping check for expired subscriptions.")
        return
    
    logger.info("Checking for expired subscriptions...")
    
    # Current time in UTC
    current_time = datetime.datetime.utcnow()
    
    # Find expired but active subscriptions
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
        
        # Try to remove user from the VIP group
        try:
            # Get user info for logging
            user_info = db.users.find_one({"chat_id": telegram_id})
            username = user_info.get("username", "Unknown") if user_info else "Unknown"
            
            # Ban the user from the group
            await context.bot.ban_chat_member(
                chat_id=VIP_CHAT_ID,
                user_id=telegram_id,
                until_date=datetime.datetime.now() + datetime.timedelta(seconds=35)  # Ban for 35 seconds (minimum time)
            )
            
            logger.info(f"Removed user {username} (ID: {telegram_id}) from VIP group due to expired subscription")
            
            # Notify the user
            try:
                await context.bot.send_message(
                    chat_id=telegram_id,
                    text="⚠️ Your subscription has expired and you have been removed from the VIP group. "
                         "Please renew your subscription to regain access."
                )
            except TelegramError as e:
                logger.error(f"Could not notify user {telegram_id} about removal: {e}")
                
        except TelegramError as e:
            logger.error(f"Failed to remove user {telegram_id} from VIP group: {e}")

def setup_scheduler(app: Application) -> None:
    """
    Set up the scheduler with the expired subscription check job
    
    Args:
        app: The Telegram Application instance
    """
    if not scheduler.running:
        # Add job to check for expired subscriptions every 24 hours
        scheduler.add_job(
            check_expired_subscriptions,
            IntervalTrigger(hours=24),
            id="check_expired_subscriptions",
            replace_existing=True,
            args=[app]
        )
        
        # Start the scheduler
        scheduler.start()
        logger.info("Scheduler started - will check for expired subscriptions every 24 hours")
        
def shutdown_scheduler() -> None:
    """Shut down the scheduler"""
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler shutdown") 