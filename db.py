import os
import pymongo
import logging
from typing import Optional, List, Dict, Any
from pymongo import MongoClient
from pymongo.database import Database
from pymongo.collection import Collection
from dotenv import load_dotenv
from datetime import datetime

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# MongoDB connection string from environment variables
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "telegram_bot_db")

# Global client variable
client = None
db = None

def init_db():
    """Initialize database connection"""
    global client, db
    try:
        client = MongoClient(MONGO_URI)
        db = client[DB_NAME]
        
        # Create indexes
        db.users.create_index("chat_id", unique=True)
        db.subscriptions.create_index("telegram_id", unique=True)
        db.subscriptions.create_index("expiry_date")  # Index for querying expired subscriptions
        
        logger.info("Connected to MongoDB")
        return db
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        raise

def get_db():
    """Get database instance"""
    global db
    if db is None:
        init_db()
    return db

def close_db():
    """Close database connection"""
    global client
    if client:
        client.close()
        logger.info("Closed MongoDB connection")

# User operations
def get_user(chat_id: int):
    """Get user by chat_id"""
    return db.users.find_one({"chat_id": chat_id})

def create_user(user_data: dict):
    """Create a new user"""
    result = db.users.insert_one(user_data)
    return result.inserted_id

def update_user(chat_id: int, update_data: dict):
    """Update user data"""
    result = db.users.update_one(
        {"chat_id": chat_id},
        {"$set": update_data}
    )
    return result.modified_count

# Subscription operations
def upsert_subscription(telegram_id: int, start_date: datetime, expiry_date: datetime, 
                        subscription_type: str = "basic", 
                        metadata: Dict[str, Any] = None) -> bool:
    """
    Insert or update a subscription for a user
    
    Args:
        telegram_id: User's Telegram ID
        start_date: Start date of subscription
        expiry_date: Expiry date of subscription
        subscription_type: Type of subscription (default: "basic")
        metadata: Additional metadata about the subscription
        
    Returns:
        bool: True if operation was successful
    """
    try:
        db = get_db()
        subscription_data = {
            "telegram_id": telegram_id,
            "start_date": start_date,
            "expiry_date": expiry_date,
            "subscription_type": subscription_type,
            "status": "active",
            "updated_at": datetime.utcnow()
        }
        
        # Add metadata if provided
        if metadata:
            subscription_data["metadata"] = metadata
            
        result = db.subscriptions.update_one(
            {"telegram_id": telegram_id},
            {"$set": subscription_data},
            upsert=True
        )
        
        logger.info(f"Subscription {'updated' if result.modified_count else 'created'} for user {telegram_id}")
        return True
    except Exception as e:
        logger.error(f"Error in upsert_subscription: {e}")
        return False

def get_subscription(telegram_id: int) -> Optional[Dict]:
    """
    Get a user's subscription
    
    Args:
        telegram_id: User's Telegram ID
        
    Returns:
        Optional[Dict]: Subscription data or None if not found
    """
    try:
        db = get_db()
        return db.subscriptions.find_one({"telegram_id": telegram_id})
    except Exception as e:
        logger.error(f"Error in get_subscription: {e}")
        return None

def find_expired_subscriptions() -> List[Dict]:
    """
    Find all subscriptions that have expired based on current UTC time
    
    Returns:
        List[Dict]: List of expired subscriptions
    """
    try:
        db = get_db()
        current_time = datetime.utcnow()
        
        # Find subscriptions where expiry_date is less than current time
        # and status is still "active"
        expired = list(db.subscriptions.find({
            "expiry_date": {"$lt": current_time},
            "status": "active"
        }))
        
        logger.info(f"Found {len(expired)} expired subscriptions")
        return expired
    except Exception as e:
        logger.error(f"Error in find_expired_subscriptions: {e}")
        return []

def mark_subscription_expired(telegram_id: int) -> bool:
    """
    Mark a subscription as expired
    
    Args:
        telegram_id: User's Telegram ID
        
    Returns:
        bool: True if operation was successful
    """
    try:
        db = get_db()
        result = db.subscriptions.update_one(
            {"telegram_id": telegram_id},
            {"$set": {
                "status": "expired",
                "updated_at": datetime.utcnow()
            }}
        )
        
        if result.modified_count:
            logger.info(f"Marked subscription as expired for user {telegram_id}")
            return True
        else:
            logger.warning(f"No active subscription found for user {telegram_id}")
            return False
    except Exception as e:
        logger.error(f"Error in mark_subscription_expired: {e}")
        return False 