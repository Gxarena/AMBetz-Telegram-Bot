import os
import logging
import asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Load environment variables and get token
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    print("\n" + "="*80)
    print("ATTENTION: Please replace 'YOUR_BOT_TOKEN_HERE' below with your actual bot token")
    print("or set the TELEGRAM_BOT_TOKEN environment variable.")
    print("="*80 + "\n")
    BOT_TOKEN = "7244340791:AAEaJyGtIbL7K8vIyLNDiHvSF25ewvS3Y-U"  # Replace with your actual token
    logger.warning("Using placeholder token. Replace it or set TELEGRAM_BOT_TOKEN environment variable.")

# Define command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Hello! I am a minimal bot.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Status: Bot is running")

# Main function
def main() -> None:
    """Start the bot."""
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.error("Please set your bot token first!")
        return
        
    # Try to avoid asyncio-related issues on Windows
    if os.name == 'nt':  # Windows
        try:
            # Use a different event loop policy on Windows
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            logger.info("Set Windows-specific event loop policy")
        except Exception as e:
            logger.error(f"Failed to set Windows event loop policy: {e}")
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    
    # Start the Bot - this will block until you press Ctrl-C
    logger.info("Starting bot...")
    application.run_polling(allowed_updates=["message"])
    
    logger.info("Bot stopped")

if __name__ == "__main__":
    main() 