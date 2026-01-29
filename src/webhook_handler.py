import os
import logging
import pytz
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import stripe
from telegram import Update
from firestore_service import FirestoreService
from gcp_stripe_service import GCPStripeService
from gcp_bot import GCPTelegramBot
from webhook_validator import WebhookValidator
import json
from datetime import datetime, timedelta
from google.cloud import secretmanager

# Configure logging
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(title="Telegram Bot Webhook Handler")

# Initialize services
project_id = os.getenv('GOOGLE_CLOUD_PROJECT')
firestore_service = FirestoreService(project_id)
stripe_service = GCPStripeService(project_id)
webhook_validator = WebhookValidator(stripe_service)

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
                
                # VALIDATE: Ensure this session was created through the bot
                validation_result = webhook_validator.validate_checkout_session(session)
                if not validation_result['valid']:
                    logger.warning(
                        "Checkout session failed validation",
                        extra={"stripe_session_id": session_id, "error": validation_result['error']}
                    )
                    webhook_validator.log_validation_failure(session_id, validation_result['error'], validation_result['action'])
                    
                    if validation_result['action'] == 'reject_payment':
                        # This is a serious security issue - someone bypassed the bot
                        logger.error(f"SECURITY ALERT: Unauthorized subscription attempt for session {session_id}")
                        # You might want to send admin alerts here
                        return JSONResponse(content={"status": "error", "message": "unauthorized_subscription"})
                    else:
                        return JSONResponse(content={"status": "success", "message": "validation_failed"})
                
                telegram_id_val = validation_result['telegram_id']
                logger.info(
                    "Checkout session passed validation",
                    extra={"telegram_id": telegram_id_val, "stripe_session_id": session_id}
                )
                
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
                    tid = subscription_data['telegram_id']
                    logger.warning(
                        "User attempted to subscribe while already active - blocking",
                        extra={"telegram_id": tid, "stripe_session_id": session_id}
                    )
                    
                    # Check if this is a duplicate webhook for the same session
                    if existing_subscription.get('stripe_session_id') == session_id:
                        logger.info(f"Duplicate webhook for session {session_id}, skipping")
                        return JSONResponse(content={"status": "success", "message": "duplicate_webhook"})
                    
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
                
                # Save subscription to Firestore
                try:
                    success = firestore_service.upsert_subscription(
                        telegram_id=subscription_data['telegram_id'],
                        start_date=subscription_data['start_date'],
                        expiry_date=subscription_data['expiry_date'],
                        subscription_type=subscription_data.get('subscription_type', 'premium'),
                        metadata=subscription_data.get('metadata'),
                        stripe_customer_id=subscription_data.get('stripe_customer_id'),
                        stripe_session_id=subscription_data.get('stripe_session_id'),
                        stripe_subscription_id=subscription_data.get('stripe_subscription_id'),
                        amount_paid=subscription_data.get('amount_paid'),
                        currency=subscription_data.get('currency')
                    )
                    
                    if success:
                        logger.info(
                            "Subscription saved to Firestore",
                            extra={"telegram_id": subscription_data['telegram_id'], "stripe_session_id": subscription_data.get('stripe_session_id')}
                        )
                        
                        # Check if this is a trial subscription
                        is_trial = subscription_data.get('subscription_type') == 'trial' or (subscription_data.get('metadata') and subscription_data['metadata'].get('is_trial'))
                        
                        # Mark user as having used trial if this is a trial subscription
                        if is_trial:
                            firestore_service.mark_trial_used(subscription_data['telegram_id'])
                            logger.info(f"Marked user {subscription_data['telegram_id']} as having used a trial")
                        
                        # Send welcome message and invite links
                        try:
                            bot_app = await get_bot_application()
                            telegram_bot_instance = GCPTelegramBot()
                            telegram_bot_instance.application = bot_app
                            
                            # Generate and send invite links
                            invite_links = await telegram_bot_instance.generate_one_time_invite_links(
                                subscription_data['telegram_id'],
                                subscription_data.get('telegram_username')
                            )
                            
                            if invite_links:
                                await telegram_bot_instance.send_vip_invite_links(
                                    subscription_data['telegram_id'],
                                    invite_links,
                                    subscription_data.get('telegram_username')
                                )
                            
                            # Send trial-specific message if applicable
                            if is_trial:
                                await bot_app.bot.send_message(
                                    chat_id=subscription_data['telegram_id'],
                                    text=f"🎉 **Free Trial Started!**\n\n"
                                         f"Your 3-day free trial is now active! You have full VIP access until:\n"
                                         f"**{subscription_data['expiry_date'].strftime('%Y-%m-%d %H:%M:%S')}**\n\n"
                                         f"After the trial ends, your subscription will automatically continue at the regular price.\n"
                                         f"You can cancel anytime before the trial ends to avoid charges.\n\n"
                                         f"Use `/status` to check your subscription anytime!"
                                )
                        except Exception as e:
                            logger.error(f"Failed to send welcome message/invite links: {e}")
                    else:
                        logger.error(f"Failed to save subscription to Firestore for user {subscription_data['telegram_id']}")
                except Exception as e:
                    logger.error(f"Error saving subscription to Firestore: {e}")
            else:
                # Handle case where subscription_data is None (no telegram_id found)
                logger.error(
                    "Failed to process payment - no subscription data (no telegram_id linked)",
                    extra={"stripe_session_id": session_id}
                )
                
                # Try to get customer info for manual intervention
                try:
                    if hasattr(session, 'customer'):
                        customer = stripe.Customer.retrieve(session.customer)
                        logger.error(f"Customer {customer.id} ({customer.email}) needs manual linking")
                        
                        # Send admin notification about failed payment
                        try:
                            from google.cloud import secretmanager
                            secret_client = secretmanager.SecretManagerServiceClient()
                            project_id = os.getenv('GOOGLE_CLOUD_PROJECT')
                            
                            secret_name = f"projects/{project_id}/secrets/admin-telegram-id/versions/latest"
                            response = secret_client.access_secret_version(request={"name": secret_name})
                            admin_ids_str = response.payload.data.decode("UTF-8").strip()
                            
                            admin_ids = [int(id_str.strip()) for id_str in admin_ids_str.split(',') if id_str.strip()]
                            
                            bot_app = await get_bot_application()
                            for admin_id in admin_ids:
                                try:
                                    await bot_app.bot.send_message(
                                        chat_id=admin_id,
                                        text=f"⚠️ **Payment Processing Failed**\n\n"
                                             f"Customer: {customer.email}\n"
                                             f"Stripe ID: {customer.id}\n"
                                             f"Session: {session_id}\n\n"
                                             f"Reason: No Telegram ID found\n"
                                             f"Action: Manual intervention required"
                                    )
                                except Exception as e:
                                    logger.error(f"Failed to send admin notification: {e}")
                        except Exception as e:
                            logger.error(f"Failed to send admin notifications: {e}")
                except Exception as e:
                    logger.error(f"Failed to get customer info: {e}")
                
                # Return success to Stripe to prevent retries, but log the issue
                return JSONResponse(content={"status": "success", "message": "payment_logged_for_manual_review"})
        
        elif event.type == 'invoice.payment_succeeded':
            logger.info("Processing invoice.payment_succeeded event (recurring payment)")
            await handle_recurring_payment(event.data.object)
        
        elif event.type == 'customer.subscription.updated':
            logger.info("Processing customer.subscription.updated event")
            # Only process subscription updates if the subscription was created via webhook
            # This prevents duplicate processing of the same subscription
            subscription = event.data.object
            if subscription.status in ['active', 'trialing']:
                await handle_subscription_updated(subscription)
            else:
                logger.info(f"Skipping subscription update for status: {subscription.status}")
        
        elif event.type == 'customer.subscription.deleted':
            logger.info("Processing customer.subscription.deleted event")
            await handle_subscription_cancelled(event.data.object)
        
        elif event.type == 'invoice.payment_failed':
            logger.info(f"Payment failed for session: {event.data.object.id}")
            await handle_payment_failed(event.data.object)
        
        else:
            logger.info(f"Unhandled event type: {event.type}")
        
        return JSONResponse(content={"status": "success"})
        
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

async def handle_recurring_payment(invoice):
    """Handle successful recurring payment"""
    try:
        logger.info(f"Processing recurring payment for invoice: {invoice.id}")
        
        # Get customer ID from invoice
        customer_id = getattr(invoice, 'customer', None)
        if not customer_id:
            logger.warning(f"No customer ID in invoice {invoice.id}")
            return
        
        # Get customer details to get telegram_id
        customer = stripe.Customer.retrieve(customer_id)
        telegram_id = customer.metadata.get('telegram_id')
        
        if not telegram_id:
            logger.warning(f"No telegram_id found in customer metadata: {customer_id} (email: {customer.email})")
            logger.info(f"Attempting to find telegram_id in Firestore subscriptions...")
            
            # Try to find telegram_id in Firestore by customer_id
            subscription = firestore_service.get_subscription_by_stripe_customer(customer_id)
            if subscription:
                telegram_id = subscription['telegram_id']
                logger.info(f"Found telegram_id in Firestore: {telegram_id}")
            else:
                logger.warning(f"No subscription found in Firestore for customer {customer_id}. Skipping webhook processing.")
                return
        
        # Try to get subscription ID from invoice
        subscription_id = getattr(invoice, 'subscription', None)
        
        # If no subscription ID at top level, try to extract from line items
        if not subscription_id and hasattr(invoice, 'lines') and invoice.lines:
            try:
                # Try to get subscription from first line item's parent field
                if hasattr(invoice.lines, 'data') and len(invoice.lines.data) > 0:
                    first_line = invoice.lines.data[0]
                    line_dict = dict(first_line)
                    
                    # Check parent -> subscription_item_details -> subscription
                    if 'parent' in line_dict and isinstance(line_dict['parent'], dict):
                        parent = line_dict['parent']
                        if 'subscription_item_details' in parent and isinstance(parent['subscription_item_details'], dict):
                            sub_id = parent['subscription_item_details'].get('subscription')
                            if sub_id:
                                subscription_id = sub_id
                                logger.info(f"Found subscription ID in line item parent: {subscription_id}")
            except Exception as e:
                logger.warning(f"Could not extract subscription from line items: {e}")
        
        # Calculate subscription dates
        from datetime import datetime, timedelta
        
        # If we have a subscription ID, use it to get accurate period dates
        if subscription_id:
            try:
                subscription = stripe.Subscription.retrieve(subscription_id)
                
                # Check if current_period_end exists
                if not hasattr(subscription, 'current_period_end') or subscription.current_period_end is None:
                    logger.warning(f"Subscription {subscription_id} has no current_period_end, using invoice date + 30 days")
                    # Fall back to invoice date + 30 days
                    # Stripe timestamps are in UTC, make timezone-aware for Firestore compatibility
                    invoice_date = datetime.fromtimestamp(invoice.created, tz=pytz.UTC)
                    current_period_start = invoice_date
                    current_period_end = invoice_date + timedelta(days=30)
                else:
                    # Stripe timestamps are in UTC, make timezone-aware for Firestore compatibility
                    current_period_start = datetime.fromtimestamp(subscription.current_period_start, tz=pytz.UTC)
                    current_period_end = datetime.fromtimestamp(subscription.current_period_end, tz=pytz.UTC)
                    
            except Exception as e:
                logger.warning(f"Could not retrieve subscription {subscription_id}: {e}, using invoice date + 30 days")
                # Stripe timestamps are in UTC, make timezone-aware for Firestore compatibility
                invoice_date = datetime.fromtimestamp(invoice.created, tz=pytz.UTC)
                current_period_start = invoice_date
                current_period_end = invoice_date + timedelta(days=30)
        else:
            # No subscription ID found - treat as one-time payment
            logger.info(f"No subscription ID found for invoice {invoice.id} - treating as one-time 30-day payment")
            # Stripe timestamps are in UTC, make timezone-aware for Firestore compatibility
            invoice_date = datetime.fromtimestamp(invoice.created, tz=pytz.UTC)
            current_period_start = invoice_date
            current_period_end = invoice_date + timedelta(days=30)
            subscription_id = None  # Explicitly set to None
        
        # Check if this was a trial conversion (first payment after trial)
        # If the previous subscription was a trial, mark user as having used trial
        existing_subscription = firestore_service.get_subscription(int(telegram_id))
        was_trial = False
        if existing_subscription:
            was_trial = (existing_subscription.get('subscription_type') == 'trial' or 
                        (existing_subscription.get('metadata') and 
                         isinstance(existing_subscription['metadata'], dict) and 
                         existing_subscription['metadata'].get('is_trial')))
        
        # Update subscription in Firestore
        success = firestore_service.upsert_subscription(
            telegram_id=int(telegram_id),
            start_date=current_period_start,
            expiry_date=current_period_end,
            subscription_type="premium",
            stripe_customer_id=customer_id,
            stripe_session_id=invoice.id,  # Use invoice ID as reference
            stripe_subscription_id=subscription_id,  # May be None for one-time payments
            amount_paid=invoice.amount_paid / 100,  # Convert from cents
            currency=invoice.currency
        )
        
        if success:
            # If this was a trial conversion, ensure user is marked as having used trial
            if was_trial:
                firestore_service.mark_trial_used(int(telegram_id))
                logger.info(f"Marked user {telegram_id} as having used trial (trial converted to paid)")
            
            logger.info(f"Updated recurring subscription for user {telegram_id}, expiry: {current_period_end}")
            
            # Notify user about successful renewal
            try:
                bot_app = await get_bot_application()
                await bot_app.bot.send_message(
                    chat_id=int(telegram_id),
                    text=f"✅ **Subscription Renewed Successfully!**\n\n"
                         f"Your VIP subscription has been renewed and will remain active until:\n"
                         f"**{current_period_end.strftime('%Y-%m-%d %H:%M:%S')}**\n\n"
                         f"Thank you for your continued support! 🎉"
                )
            except Exception as e:
                logger.error(f"Failed to send renewal notification to user {telegram_id}: {e}")
        else:
            logger.error(f"Failed to update recurring subscription for user {telegram_id}")
            
    except Exception as e:
        logger.error(f"Error handling recurring payment: {e}", exc_info=True)

async def handle_subscription_updated(subscription):
    """Handle subscription updates (status changes, etc.)"""
    try:
        logger.info(f"Processing subscription update: {subscription.id}")
        
        # Get customer details
        customer = stripe.Customer.retrieve(subscription.customer)
        telegram_id = customer.metadata.get('telegram_id')
        
        if not telegram_id:
            logger.warning(f"No telegram_id found in customer metadata: {subscription.customer} (email: {customer.email})")
            logger.info(f"Attempting to find telegram_id in Firestore subscriptions...")
            
            # Try to find telegram_id in Firestore by customer_id
            subscription_data = firestore_service.get_subscription_by_stripe_customer(subscription.customer)
            if subscription_data:
                telegram_id = subscription_data['telegram_id']
                logger.info(f"Found telegram_id in Firestore: {telegram_id}")
            else:
                logger.warning(f"No subscription found in Firestore for customer {subscription.customer}. Skipping webhook processing.")
                return
        
        # Check if this is a new subscription that was just created
        # If so, we should let the checkout.session.completed handler deal with it
        existing_subscription = firestore_service.get_subscription(int(telegram_id))
        if not existing_subscription:
            logger.info(f"No existing subscription found for user {telegram_id}, this might be a new subscription. Skipping update.")
            return
        
        # Check if subscription is still active
        if subscription.status in ['active', 'trialing']:
            # Subscription is active, update expiry date
            # Check if current_period_end exists
            if not hasattr(subscription, 'current_period_end') or subscription.current_period_end is None:
                logger.warning(f"Subscription {subscription.id} has no current_period_end, skipping update")
                return
            
            # Stripe timestamps are in UTC, make timezone-aware for Firestore compatibility
            current_period_end = datetime.fromtimestamp(subscription.current_period_end, tz=pytz.UTC)
            
            # Only update if the expiry date has actually changed
            if existing_subscription.get('expiry_date') != current_period_end:
                success = firestore_service.upsert_subscription(
                    telegram_id=int(telegram_id),
                    start_date=datetime.fromtimestamp(subscription.current_period_start, tz=pytz.UTC),
                    expiry_date=current_period_end,
                    subscription_type="premium",
                    stripe_customer_id=subscription.customer,
                    stripe_session_id=subscription.id,
                    stripe_subscription_id=subscription.id
                )
                
                if success:
                    logger.info(f"Updated subscription status for user {telegram_id}")
            else:
                logger.info(f"Subscription expiry date unchanged for user {telegram_id}, skipping update")
        else:
            # Subscription is not active, mark as expired
            success = firestore_service.mark_subscription_expired(int(telegram_id))
            if success:
                logger.info(f"Marked subscription as expired for user {telegram_id}")
                
                # Notify user about subscription status change
                try:
                    bot_app = await get_bot_application()
                    await bot_app.bot.send_message(
                        chat_id=int(telegram_id),
                        text=f"⚠️ **Subscription Status Changed**\n\n"
                             f"Your subscription status has been updated to: **{subscription.status}**\n\n"
                             f"Please check your subscription status with /status"
                    )
                except Exception as e:
                    logger.error(f"Failed to send status update notification: {e}")
                    
    except Exception as e:
        logger.error(f"Error handling subscription update: {e}", exc_info=True)

async def handle_subscription_cancelled(subscription):
    """Handle subscription cancellation"""
    try:
        logger.info(f"Processing subscription cancellation: {subscription.id}")
        
        # Get customer details
        customer = stripe.Customer.retrieve(subscription.customer)
        telegram_id = customer.metadata.get('telegram_id')
        
        if not telegram_id:
            logger.warning(f"No telegram_id found in customer metadata: {subscription.customer} (email: {customer.email})")
            logger.info(f"Attempting to find telegram_id in Firestore subscriptions...")
            
            # Try to find telegram_id in Firestore by customer_id
            subscription_data = firestore_service.get_subscription_by_stripe_customer(subscription.customer)
            if subscription_data:
                telegram_id = subscription_data['telegram_id']
                logger.info(f"Found telegram_id in Firestore: {telegram_id}")
            else:
                logger.warning(f"No subscription found in Firestore for customer {subscription.customer}. Skipping webhook processing.")
                return
        
        # Calculate when subscription actually expires (end of current period)
        # Guard: Stripe subscription.deleted object may have current_period_end missing or None
        current_period_end = None
        try:
            period_end_ts = getattr(subscription, 'current_period_end', None)
            if period_end_ts is not None:
                current_period_end = datetime.fromtimestamp(period_end_ts, tz=pytz.UTC)
        except (TypeError, ValueError) as e:
            logger.warning(f"Could not parse current_period_end for subscription {subscription.id}: {e}")
        if current_period_end is None:
            # Fallback: use existing Firestore expiry or now
            existing = firestore_service.get_subscription(int(telegram_id))
            if existing and existing.get('expiry_date'):
                current_period_end = existing['expiry_date']
                if hasattr(current_period_end, 'tzinfo') and current_period_end.tzinfo is None:
                    current_period_end = pytz.UTC.localize(current_period_end)
            else:
                current_period_end = datetime.now(pytz.UTC)
            logger.info(f"Using fallback expiry for subscription.deleted: {current_period_end}")

        cancellation_metadata = {"cancelled": True, "cancelled_at": datetime.utcnow().isoformat()}

        # IMPORTANT: subscription.deleted means the subscription has ENDED. Set status to 'expired'
        # so the user can resubscribe. Using upsert_subscription would set status='active' and block
        # resubscription (e.g. if cron already marked them expired, we'd overwrite back to active).
        success = firestore_service.set_subscription_cancelled_expired(
            telegram_id=int(telegram_id),
            expiry_date=current_period_end,
            metadata=cancellation_metadata,
            stripe_customer_id=subscription.customer,
            stripe_subscription_id=subscription.id,
        )
        if not success:
            # Fallback: doc may not exist (rare); or update failed - at least mark expired so resubscription works
            existing = firestore_service.get_subscription(int(telegram_id))
            if existing:
                firestore_service.mark_subscription_expired(int(telegram_id))
                logger.info(f"Fallback: marked subscription expired for user {telegram_id} (resubscription allowed)")
                success = True
            else:
                logger.warning(f"No subscription doc for user {telegram_id} on subscription.deleted - cannot update")
        else:
            logger.info(f"Set cancelled+expired for user {telegram_id} - resubscription allowed")
            
            # Get user info for display name
            user_info = firestore_service.get_user(int(telegram_id))
            
            # Determine display name (username preferred, otherwise first/last name)
            if user_info and user_info.get('username'):
                display_name = f"@{user_info['username']}"
            elif user_info:
                first_name = user_info.get('first_name', '')
                last_name = user_info.get('last_name', '')
                if first_name or last_name:
                    display_name = f"{first_name} {last_name}".strip()
                else:
                    display_name = f"User {telegram_id}"
            else:
                display_name = f"User {telegram_id}"
            
            # Notify user about cancellation
            try:
                bot_app = await get_bot_application()
                await bot_app.bot.send_message(
                    chat_id=int(telegram_id),
                    text=f"❌ **Subscription Cancelled**\n\n"
                         f"Your subscription has been cancelled and will expire on:\n"
                         f"**{current_period_end.strftime('%Y-%m-%d %H:%M:%S')}**\n\n"
                         f"You will continue to have VIP access until then.\n\n"
                         f"Use /start to resubscribe when you're ready to return!"
                )
            except Exception as e:
                logger.error(f"Failed to send cancellation notification: {e}")
            
            # Notify admin about the cancellation
            try:
                secret_client = secretmanager.SecretManagerServiceClient()
                project_id = os.getenv('GOOGLE_CLOUD_PROJECT')
                
                # Get admin telegram IDs
                try:
                    secret_name = f"projects/{project_id}/secrets/admin-telegram-id/versions/latest"
                    response = secret_client.access_secret_version(request={"name": secret_name})
                    admin_ids_str = response.payload.data.decode("UTF-8").strip()
                    
                    # Parse comma-separated admin IDs
                    admin_ids = [int(id_str.strip()) for id_str in admin_ids_str.split(',') if id_str.strip()]
                    
                    # Send admin notification to all admins
                    bot_app = await get_bot_application()
                    for admin_id in admin_ids:
                        try:
                            await bot_app.bot.send_message(
                                chat_id=admin_id,
                                text=f"⚠️ **Subscription Cancelled**\n\n"
                                     f"User: {display_name}\n"
                                     f"Telegram ID: {telegram_id}\n"
                                     f"Expires: {current_period_end.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                                     f"They will retain access until the expiry date."
                            )
                            logger.info(f"Sent cancellation notification to admin {admin_id} for user {display_name}")
                        except Exception as e:
                            logger.error(f"Failed to send cancellation notification to admin {admin_id}: {e}")
                except Exception as e:
                    logger.error(f"Failed to send admin notification about cancellation: {e}")
            except Exception as e:
                logger.error(f"Error setting up admin notification: {e}")
                
    except Exception as e:
        logger.error(f"Error handling subscription cancellation: {e}", exc_info=True)

async def handle_payment_failed(invoice):
    """Handle failed payment by canceling the subscription"""
    try:
        logger.info(f"Processing failed payment for invoice: {invoice.id}")
        
        # Get subscription from invoice
        subscription_id = getattr(invoice, 'subscription', None)
        if not subscription_id:
            logger.warning(f"No subscription ID in invoice {invoice.id} - likely a one-time payment")
            return
        
        # Get subscription details
        subscription = stripe.Subscription.retrieve(subscription_id)
        customer = stripe.Customer.retrieve(subscription.customer)
        telegram_id = customer.metadata.get('telegram_id')
        
        if not telegram_id:
            logger.warning(f"No telegram_id found in customer metadata: {subscription.customer} (email: {customer.email})")
            logger.info(f"Attempting to find telegram_id in Firestore subscriptions...")
            
            # Try to find telegram_id in Firestore by customer_id
            subscription_data = firestore_service.get_subscription_by_stripe_customer(subscription.customer)
            if subscription_data:
                telegram_id = subscription_data['telegram_id']
                logger.info(f"Found telegram_id in Firestore: {telegram_id}")
            else:
                logger.warning(f"No subscription found in Firestore for customer {subscription.customer}. Skipping webhook processing.")
                return
        
        # Cancel the subscription due to payment failure
        try:
            logger.info(f"Canceling subscription {subscription_id} due to payment failure")
            canceled_subscription = stripe.Subscription.cancel(subscription_id)
            logger.info(f"Successfully canceled subscription {subscription_id}")
        except Exception as e:
            logger.error(f"Failed to cancel subscription {subscription_id}: {e}")
            return
        
        # Mark subscription as expired in Firestore
        try:
            firestore_service.mark_subscription_expired(int(telegram_id))
            logger.info(f"Marked subscription as expired for user {telegram_id}")
        except Exception as e:
            logger.error(f"Failed to mark subscription expired for user {telegram_id}: {e}")
        
        # Get bot application for kicking and notifications
        try:
            bot_app = await get_bot_application()
        except Exception as e:
            logger.error(f"Failed to get bot application: {e}")
            return
        
        # KICK USER FROM VIP GROUPS
        # Get user info for display name
        user_info = firestore_service.get_user(int(telegram_id))
        if user_info and user_info.get('username'):
            display_name = f"@{user_info['username']}"
        elif user_info:
            first_name = user_info.get('first_name', '')
            last_name = user_info.get('last_name', '')
            if first_name or last_name:
                display_name = f"{first_name} {last_name}".strip()
            else:
                display_name = f"User {telegram_id}"
        else:
            display_name = f"User {telegram_id}"
        
        # Try to remove from VIP announcements channel
        vip_announcements_id_str = None
        vip_discussion_id_str = None
        
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
                    user_id=int(telegram_id)
                )
                # Unban immediately (this removes from group but allows rejoining later)
                await bot_app.bot.unban_chat_member(
                    chat_id=vip_announcements_id,
                    user_id=int(telegram_id)
                )
                logger.info(f"Removed user {telegram_id} from VIP announcements group (payment failed)")
            except Exception as e:
                if "supergroup and channel chats only" in str(e):
                    logger.warning(f"VIP announcements group is a regular group, cannot auto-remove user {telegram_id}.")
                else:
                    logger.error(f"Failed to remove user {telegram_id} from VIP announcements group: {e}")
        
        # Try to remove from VIP discussion group
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
                    user_id=int(telegram_id)
                )
                # Unban immediately (this removes from group but allows rejoining later)
                await bot_app.bot.unban_chat_member(
                    chat_id=vip_discussion_id,
                    user_id=int(telegram_id)
                )
                logger.info(f"Removed user {telegram_id} from VIP discussion group (payment failed)")
            except Exception as e:
                if "supergroup and channel chats only" in str(e):
                    logger.warning(f"VIP discussion group is a regular group, cannot auto-remove user {telegram_id}.")
                else:
                    logger.error(f"Failed to remove user {telegram_id} from VIP discussion group: {e}")
        
        # Notify user about failed payment and cancellation
        try:
            await bot_app.bot.send_message(
                chat_id=int(telegram_id),
                text=f"❌ **Payment Failed - Subscription Cancelled**\n\n"
                     f"Your subscription payment could not be processed and your subscription has been cancelled.\n\n"
                     f"You have been removed from the VIP groups.\n\n"
                     f"You can resubscribe anytime using /start to regain VIP access."
            )
        except Exception as e:
            logger.error(f"Failed to send payment failure notification: {e}")
        
        # Notify admins about payment failure and removal
        try:
            secret_name = f"projects/{project_id}/secrets/admin-telegram-id/versions/latest"
            response = client.access_secret_version(request={"name": secret_name})
            admin_ids_str = response.payload.data.decode("UTF-8").strip()
            
            # Parse comma-separated admin IDs
            admin_ids = [int(id_str.strip()) for id_str in admin_ids_str.split(',') if id_str.strip()]
            
            # Send notification to all admins
            for admin_id in admin_ids:
                try:
                    await bot_app.bot.send_message(
                        chat_id=admin_id,
                        text=f"⚠️ **Payment Failed - User Removed**\n\n"
                             f"User: {display_name}\n"
                             f"Telegram ID: {telegram_id}\n"
                             f"Reason: Payment failed, subscription cancelled\n\n"
                             f"User has been removed from VIP groups."
                    )
                    logger.info(f"Sent payment failure notification to admin {admin_id} for user {display_name}")
                except Exception as e:
                    logger.error(f"Failed to send payment failure notification to admin {admin_id}: {e}")
        except Exception as e:
            logger.error(f"Failed to send admin notifications about payment failure: {e}")
            
    except Exception as e:
        logger.error(f"Error handling payment failure: {e}", exc_info=True)

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
                
                # Determine display name (username preferred, otherwise first/last name)
                if user_info and user_info.get('username'):
                    display_name = f"@{user_info['username']}"
                elif user_info:
                    first_name = user_info.get('first_name', '')
                    last_name = user_info.get('last_name', '')
                    if first_name or last_name:
                        display_name = f"{first_name} {last_name}".strip()
                    else:
                        display_name = f"User {telegram_id}"
                else:
                    display_name = f"User {telegram_id}"
                
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
                        # Regular groups don't support ban_chat_member, only supergroups
                        if "supergroup and channel chats only" in str(e):
                            logger.warning(f"VIP announcements group is a regular group, cannot auto-remove user {telegram_id}. Convert to supergroup for auto-kick.")
                        else:
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
                        # Regular groups don't support ban_chat_member, only supergroups
                        if "supergroup and channel chats only" in str(e):
                            logger.warning(f"VIP discussion group is a regular group, cannot auto-remove user {telegram_id}. Convert to supergroup for auto-kick.")
                        else:
                            logger.error(f"Failed to remove user {telegram_id} from VIP discussion group: {e}")
                
                # Send expiry notification to user
                try:
                    await bot_app.bot.send_message(
                        chat_id=telegram_id,
                        text="⚠️ Your subscription has expired and you have been removed from the VIP groups. Use /start to renew your subscription."
                    )
                except Exception as e:
                    logger.error(f"Failed to send expiry notification to user {telegram_id}: {e}")
                
                # Send admin notification to all admins
                try:
                    secret_name = f"projects/{project_id}/secrets/admin-telegram-id/versions/latest"
                    response = client.access_secret_version(request={"name": secret_name})
                    admin_ids_str = response.payload.data.decode("UTF-8").strip()
                    
                    # Parse comma-separated admin IDs
                    admin_ids = [int(id_str.strip()) for id_str in admin_ids_str.split(',') if id_str.strip()]
                    
                    # Send notification to all admins
                    for admin_id in admin_ids:
                        try:
                            await bot_app.bot.send_message(
                                chat_id=admin_id,
                                text=f"🚫 **User Removed from VIP Groups**\n\n"
                                     f"User: {display_name}\n"
                                     f"Telegram ID: {telegram_id}\n"
                                     f"Reason: Subscription expired"
                            )
                            logger.info(f"Sent kick notification to admin {admin_id} for user {display_name}")
                        except Exception as e:
                            logger.error(f"Failed to send kick notification to admin {admin_id}: {e}")
                except Exception as e:
                    logger.error(f"Failed to send admin notifications: {e}")
                
                kicked_count += 1
                logger.info(f"Successfully processed expired subscription for user {display_name} ({telegram_id})")
                
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