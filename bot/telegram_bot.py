import logging
import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, filters

# Load environment variables from .env file
load_dotenv()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please DM me to access subscription commands.")
        return
    await update.message.reply_text("Welcome to our subscription service! Use /subscribe to get started.")

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please use this command in a private message.")
        return
    user_id = update.effective_user.id
    fastapi_endpoint = "http://127.0.0.1:8000"
    subscription_url = f"{fastapi_endpoint}/paypal/create_subscription?user_id={user_id}"
    await update.message.reply_text(f"Click here to subscribe: {subscription_url}")

async def main():
    # Get the token from the environment variable
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("No Telegram bot token found in environment variables!")
        
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("subscribe", subscribe, filters=filters.ChatType.PRIVATE))
    await application.run_polling()

if __name__ == "__main__":
    # Use nest_asyncio to patch the event loop if running in an environment that already has one (e.g., Jupyter or certain shells)
    import nest_asyncio
    nest_asyncio.apply()
    
    import asyncio
    asyncio.run(main())
