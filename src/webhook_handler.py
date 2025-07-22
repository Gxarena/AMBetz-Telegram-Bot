import os
import logging
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import stripe
from telegram import Update
from firestore_service import FirestoreService
from gcp_stripe_service import GCPStripeService
from gcp_bot import GCPTelegramBot
import json
from datetime import timedelta

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
            logger.info(f"Raw payload type: {type(payload)}")
            logger.info(f"Raw payload: {payload.decode('utf-8')}")  # First 500 chars
            
            event_dict = json.loads(payload.decode('utf-8'))
            event = stripe.Event.construct_from(event_dict, stripe.api_key)

            logger.info(f"Decoded event_dict keys: {list(event_dict.keys())}")

            logger.info(f"Event type: {type(event)}")
            logger.info(f"Event data type: {type(event.data)}")
            logger.info(f"Event data object type: {type(event.data.object)}")
        except ValueError as e:
            logger.error(f"Invalid payload: {e}")
            raise HTTPException(status_code=400, detail="Invalid payload")
        
        # Handle the event
        if event.type == 'checkout.session.completed':
            logger.info("Processing checkout.session.completed event")
            logger.info(f"Event data type: {type(event.data)}")
            logger.info(f"Event data object type: {type(event.data.object)}")
            
            try:
                session = event.data.object
                logger.info(f"Session object type: {type(session)}")
                logger.info(f"Session object attributes: {dir(session)}")
                
                session_id = session.id
                logger.info(f"Session ID: {session_id}")
                
                # Check if this session was already processed to prevent duplicates
                existing_subscription = firestore_service.get_subscription_by_stripe_session(session_id)
                if existing_subscription:
                    logger.info(f"Session {session_id} already processed, skipping duplicate")
                    return JSONResponse(content={"status": "success", "message": "already_processed"})
                    
            except Exception as e:
                logger.error(f"Error accessing session object: {e}")
                logger.error(f"Event data: {event.data}")
                raise
            
            try:
                # Process the successful payment
                subscription_data = stripe_service.handle_successful_payment(session)
                logger.info(f"Subscription data: {subscription_data}")
            except Exception as e:
                logger.error(f"Error in handle_successful_payment: {e}")
                logger.error(f"Session object: {session}")
                raise
            
            if subscription_data:
                # Check if user already has an active subscription
                existing_subscription = firestore_service.get_subscription(subscription_data['telegram_id'])
                if existing_subscription and existing_subscription.get('status') == 'active':
                    logger.warning(f"User {subscription_data['telegram_id']} attempted to subscribe while already having active subscription")
                    
                    # Send message to user explaining they can't subscribe again
                    try:
                        bot_app = await get_bot_application()
                        expiry_date = existing_subscription['expiry_date']
                        await bot_app.bot.send_message(
                            chat_id=subscription_data['telegram_id'],
                            text=f"❌ **Subscription Already Active**\n\n"
                                 f"You already have an active subscription that expires on:\n"
                                 f"**{expiry_date.strftime('%Y-%m-%d %H:%M:%S')}**\n\n"
                                 f"You cannot subscribe again until your current subscription expires.\n\n"
                                 f"Use `/status` to check your current subscription."
                        )
                    except Exception as e:
                        logger.error(f"Failed to send subscription blocked message: {e}")
                    
                    # Return success to Stripe but don't create new subscription
                    return JSONResponse(content={"status": "success", "message": "subscription_blocked"})
                
                # Save subscription to Firestore (only if no active subscription exists)
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
                        logger.info(f"Getting user info for telegram_id: {subscription_data['telegram_id']}")
                        user_info = firestore_service.get_user(subscription_data['telegram_id'])
                        logger.info(f"User info type: {type(user_info)}")
                        logger.info(f"User info content: {user_info}")
                        
                        try:
                            username = user_info.get("username") if user_info else None
                            logger.info(f"Username extracted: {username}")
                        except Exception as e:
                            logger.error(f"Error getting username from user_info: {e}")
                            logger.error(f"User info type: {type(user_info)}")
                            logger.error(f"User info content: {user_info}")
                            username = None
                        
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
                                text="🎉 Welcome to AMBetz VIP! Your subscription is active. Please contact AM for your invite links."
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
        
        if not expired_subscriptions:
            logger.info("No expired subscriptions found")
            return JSONResponse(content={
                "status": "success",
                "expired_count": 0,
                "message": "No expired subscriptions found"
            })
        
        # Get bot application
        bot_app = await get_bot_application()
        
        # Process each expired subscription
        kicked_count = 0
        for subscription in expired_subscriptions:
            telegram_id = subscription.get('telegram_id')
            if not telegram_id:
                continue
                
            try:
                # Mark as expired in Firestore
                success = firestore_service.mark_subscription_expired(telegram_id)
                if not success:
                    logger.error(f"Failed to mark subscription expired for user {telegram_id}")
                    continue
                
                # Get user info for notifications
                user_info = firestore_service.get_user(telegram_id)
                username = user_info.get("username") if user_info else None
                
                # Try to remove from VIP announcements channel
                vip_announcements_id_str = firestore_service._get_secret("vip-announcements-id") if hasattr(firestore_service, '_get_secret') else None
                if not vip_announcements_id_str:
                    # Get from secret manager directly
                    from google.cloud import secretmanager
                    client = secretmanager.SecretManagerServiceClient()
                    project_id = os.getenv('GOOGLE_CLOUD_PROJECT')
                    try:
                        secret_name = f"projects/{project_id}/secrets/vip-announcements-id/versions/latest"
                        response = client.access_secret_version(request={"name": secret_name})
                        vip_announcements_id_str = response.payload.data.decode("UTF-8").strip()
                    except Exception:
                        vip_announcements_id_str = None
                
                if vip_announcements_id_str:
                    try:
                        vip_announcements_id = int(vip_announcements_id_str)
                        await bot_app.bot.ban_chat_member(
                            chat_id=vip_announcements_id,
                            user_id=telegram_id
                        )
                        # Unban immediately (this removes from group but allows rejoining later)
                        await bot_app.bot.unban_chat_member(
                            chat_id=vip_announcements_id,
                            user_id=telegram_id
                        )
                        logger.info(f"Removed user {telegram_id} from VIP announcements group")
                    except Exception as e:
                        logger.error(f"Failed to remove user {telegram_id} from VIP announcements group: {e}")
                
                # Try to remove from VIP discussion group
                vip_discussion_id_str = firestore_service._get_secret("vip-chat-id") if hasattr(firestore_service, '_get_secret') else None
                if not vip_discussion_id_str:
                    try:
                        secret_name = f"projects/{project_id}/secrets/vip-chat-id/versions/latest"
                        response = client.access_secret_version(request={"name": secret_name})
                        vip_discussion_id_str = response.payload.data.decode("UTF-8").strip()
                    except Exception:
                        vip_discussion_id_str = None
                
                if vip_discussion_id_str:
                    try:
                        vip_discussion_id = int(vip_discussion_id_str)
                        await bot_app.bot.ban_chat_member(
                            chat_id=vip_discussion_id,
                            user_id=telegram_id
                        )
                        # Unban immediately (this removes from group but allows rejoining later)
                        await bot_app.bot.unban_chat_member(
                            chat_id=vip_discussion_id,
                            user_id=telegram_id
                        )
                        logger.info(f"Removed user {telegram_id} from VIP discussion group")
                    except Exception as e:
                        logger.error(f"Failed to remove user {telegram_id} from VIP discussion group: {e}")
                
                # Send expiry notification to user
                try:
                    await bot_app.bot.send_message(
                        chat_id=telegram_id,
                        text="⚠️ Your subscription has expired and you have been removed from the VIP groups. Use /start to renew your subscription."
                    )
                except Exception as e:
                    logger.error(f"Failed to send expiry notification to user {telegram_id}: {e}")
                
                # Send admin notification
                admin_id_str = None
                try:
                    secret_name = f"projects/{project_id}/secrets/admin-telegram-id/versions/latest"
                    response = client.access_secret_version(request={"name": secret_name})
                    admin_id_str = response.payload.data.decode("UTF-8").strip()
                except Exception:
                    pass
                
                if admin_id_str:
                    try:
                        admin_id = int(admin_id_str)
                        await bot_app.bot.send_message(
                            chat_id=admin_id,
                            text=f"🚫 User {username or telegram_id} has been removed from VIP groups due to subscription expiry."
                        )
                    except Exception as e:
                        logger.error(f"Failed to send admin notification: {e}")
                
                kicked_count += 1
                logger.info(f"Successfully processed expired subscription for user {telegram_id}")
                
            except Exception as e:
                logger.error(f"Error processing expired subscription for user {telegram_id}: {e}")
        
        logger.info(f"Processed {len(expired_subscriptions)} expired subscriptions, kicked {kicked_count} users")
        
        return JSONResponse(content={
            "status": "success",
            "expired_count": len(expired_subscriptions),
            "kicked_count": kicked_count,
            "message": f"Processed {len(expired_subscriptions)} expired subscriptions"
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