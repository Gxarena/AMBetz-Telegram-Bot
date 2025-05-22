from fastapi import FastAPI, Request, Response, HTTPException
import uvicorn
from bot import init_bot, setup_bot, process_update
from db import init_db
from scheduler import setup_scheduler, shutdown_scheduler
import os
import threading
import asyncio
from dotenv import load_dotenv
import logging
import nest_asyncio

# Apply nest_asyncio to allow nested event loops
nest_asyncio.apply()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Load environment variables from .env file
# Required variables:
# - TELEGRAM_BOT_TOKEN: Your Telegram bot token from BotFather
# - WEBHOOK_URL: Public URL for webhook (e.g., https://your-domain.com)
# - VIP_CHAT_ID: ID of the VIP Telegram group
# - MONGO_URI: MongoDB connection string (default: mongodb://localhost:27017)
# - DB_NAME: MongoDB database name (default: telegram_bot_db)
load_dotenv()

# Verify essential environment variables
if not os.getenv("TELEGRAM_BOT_TOKEN"):
    logger.error("TELEGRAM_BOT_TOKEN environment variable is not set!")

# Initialize FastAPI app
app = FastAPI(title="Telegram Bot API")

# Store the bot application
bot_app = None

# Ensure database is initialized on startup
@app.on_event("startup")
def startup():
    try:
        # Initialize database
        init_db()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")

# Cleanup on shutdown
@app.on_event("shutdown")
def shutdown():
    # Shutdown the scheduler
    shutdown_scheduler()
    logger.info("FastAPI application shutdown complete")

# Telegram bot webhook endpoint (still useful for production)
@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    await process_update(data)
    return Response(status_code=200)

# Health check endpoint
@app.get("/health")
def health_check():
    return {"status": "ok"}

# Function to run the bot in a separate process
def run_bot():
    """Run the Telegram bot in a separate process"""
    # Create a new event loop for this process
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # Initialize the bot in this process
    bot_app = init_bot()
    logger.info("Bot initialized successfully")
    
    # Setup scheduler
    setup_scheduler(bot_app)
    logger.info("Scheduler setup completed")
    
    # Run the bot with polling
    async def start_polling():
        logger.info("Starting bot polling...")
        # No need to call initialize separately, run_polling handles it
        await bot_app.run_polling(allowed_updates=["message", "callback_query"])
        
    # Run the polling coroutine
    try:
        loop.run_until_complete(start_polling())
    except KeyboardInterrupt:
        pass
    finally:
        # Shutdown will be handled automatically by run_polling
        loop.close()

def run_fastapi():
    """Run the FastAPI app"""
    logger.info("Starting FastAPI server on port 8000")
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
    
if __name__ == "__main__":
    # Start the bot in a separate process using multiprocessing
    # This is more reliable than threading for this use case
    import multiprocessing
    
    # Fix for Windows: set the start method to spawn
    if os.name == 'nt':  # Windows
        multiprocessing.set_start_method('spawn', force=True)
    
    # Create and start the bot process
    bot_process = multiprocessing.Process(target=run_bot)
    bot_process.daemon = True  # Process will terminate when main process ends
    bot_process.start()
    logger.info(f"Bot process started with PID {bot_process.pid}")
    
    # Run FastAPI in the main process
    run_fastapi() 