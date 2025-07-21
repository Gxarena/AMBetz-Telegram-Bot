import os
import logging
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import stripe
from telegram import Update
from firestore_service import FirestoreService
from gcp_stripe_service import GCPStripeService
from gcp_bot import GCPTelegramBot

# Configure logging
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(title="Telegram Bot Webhook Handler")

# Initialize services
project_id = os.getenv('GOOGLE_CLOUD_PROJECT')
firestore_service = FirestoreService(project_id)
stripe_service = GCPStripeService(project_id)

# Initialize Telegram bot (will be done lazily in webhook function)
telegram_bot = None
bot_application = None

async def get_bot_application():
    """Lazily initialize the bot application"""
    global telegram_bot, bot_application
    if bot_application is None:
        telegram_bot = GCPTelegramBot()
        bot_application = telegram_bot.setup_application()
        # Initialize the application for webhook mode
        await bot_application.initialize()
        logger.info("Bot application initialized successfully")
    return bot_application

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "telegram-bot-webhook"}

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    """Handle Telegram webhook updates"""
    try:
        # Get the update data
        update_data = await request.json()
        logger.info(f"Received webhook update: {update_data}")
        
        # Get the bot application (lazy initialization)
        app = await get_bot_application()
        
        # Create Update object
        update = Update.de_json(update_data, app.bot)
        if update is None:
            logger.error(f"Failed to create Update object from: {update_data}")
            return JSONResponse(content={"status": "ok"})
        
        logger.info(f"Processing update ID: {update.update_id}")
        
        # Process the update through the bot's handlers
        await app.process_update(update)
        
        logger.info(f"Successfully processed update ID: {update.update_id}")
        return JSONResponse(content={"status": "ok"})
        
    except Exception as e:
        logger.error(f"Error processing Telegram update: {e}", exc_info=True)
        # Don't return error to Telegram (it will keep retrying)
        return JSONResponse(content={"status": "ok"})

@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events"""
    # Check if Stripe is configured
    if not stripe_service.is_configured:
        logger.warning("Stripe webhook called but Stripe is not configured")
        raise HTTPException(status_code=503, detail="Stripe not configured")
        
    try:
        # Get the raw body and signature
        payload = await request.body()
        signature = request.headers.get('stripe-signature')
        
        if not signature:
            logger.error("Missing Stripe signature")
            raise HTTPException(status_code=400, detail="Missing signature")
        
        # Verify the webhook signature
        if not stripe_service.verify_webhook_signature(payload, signature):
            logger.error("Invalid webhook signature")
            raise HTTPException(status_code=400, detail="Invalid signature")
        
        # Parse the event
        try:
            event = stripe.Event.construct_from(
                payload.decode('utf-8'), stripe.api_key
            )
        except ValueError as e:
            logger.error(f"Invalid payload: {e}")
            raise HTTPException(status_code=400, detail="Invalid payload")
        
        # Handle the event
        if event.type == 'checkout.session.completed':
            session = event.data.object
            logger.info(f"Payment completed for session: {session.id}")
            logger.info(f"Session data type: {type(session)}")
            logger.info(f"Session data: {session}")
            
            try:
                # Process the successful payment
                subscription_data = stripe_service.handle_successful_payment(session)
                logger.info(f"Subscription data: {subscription_data}")
            except Exception as e:
                logger.error(f"Error in handle_successful_payment: {e}")
                logger.error(f"Session object: {session}")
                raise
            
            if subscription_data:
                # Save subscription to Firestore
                success = firestore_service.upsert_subscription(
                    telegram_id=subscription_data['telegram_id'],
                    start_date=subscription_data['start_date'],
                    expiry_date=subscription_data['expiry_date'],
                    subscription_type=subscription_data['subscription_type'],
                    stripe_customer_id=subscription_data['stripe_customer_id'],
                    stripe_session_id=subscription_data['stripe_session_id'],
                    amount_paid=subscription_data['amount_paid'],
                    currency=subscription_data['currency']
                )
                
                if success:
                    logger.info(f"Subscription created for user {subscription_data['telegram_id']}")
                    
                    # Generate and send one-time invite links
                    try:
                        bot_app = await get_bot_application()
                        
                        # Get user info for username
                        user_info = firestore_service.get_user(subscription_data['telegram_id'])
                        username = user_info.get("username") if user_info else None
                        
                        # Create bot instance and set up application
                        telegram_bot = GCPTelegramBot()
                        telegram_bot.application = bot_app
                        
                        # Generate one-time invite links
                        invite_links = await telegram_bot.generate_one_time_invite_links(
                            subscription_data['telegram_id'], 
                            username
                        )
                        
                        # Send invite links to user
                        await telegram_bot.send_vip_invite_links(
                            subscription_data['telegram_id'],
                            invite_links,
                            username
                        )
                        
                        logger.info(f"Sent invite links to user {subscription_data['telegram_id']}")
                        
                    except Exception as e:
                        logger.error(f"Failed to send invite links to user {subscription_data['telegram_id']}: {e}")
                        # Send fallback message
                        try:
                            await bot_app.bot.send_message(
                                chat_id=subscription_data['telegram_id'],
                                text="🎉 Welcome to AMBetz VIP! Your subscription is active. Please contact support for your invite links."
                            )
                        except Exception as fallback_error:
                            logger.error(f"Failed to send fallback message: {fallback_error}")
                else:
                    logger.error(f"Failed to save subscription for user {subscription_data['telegram_id']}")
            else:
                logger.error("Failed to process payment data")
        
        elif event.type == 'invoice.payment_failed':
            logger.info(f"Payment failed for session: {event.data.object.id}")
            # Handle failed payment if needed
        
        else:
            logger.info(f"Unhandled event type: {event.type}")
        
        return JSONResponse(content={"status": "success"})
        
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/check-expired")
async def check_expired_subscriptions():
    """Endpoint to manually trigger expired subscription check"""
    try:
        # Find expired subscriptions
        expired_subscriptions = firestore_service.find_expired_subscriptions()
        
        # Mark them as expired
        updated_count = 0
        for subscription in expired_subscriptions:
            telegram_id = subscription.get('telegram_id')
            if telegram_id:
                success = firestore_service.mark_subscription_expired(telegram_id)
                if success:
                    updated_count += 1
        
        logger.info(f"Processed {updated_count} expired subscriptions")
        
        return JSONResponse(content={
            "status": "success",
            "expired_count": len(expired_subscriptions),
            "updated_count": updated_count
        })
        
    except Exception as e:
        logger.error(f"Error checking expired subscriptions: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.on_event("shutdown")
async def shutdown_event():
    """Clean up resources on shutdown"""
    global bot_application
    if bot_application:
        try:
            await bot_application.shutdown()
            logger.info("Bot application shut down successfully")
        except Exception as e:
            logger.error(f"Error shutting down bot application: {e}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port) 