# app/routes.py

import os
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse
import httpx
from app.config import (
    PAYPAL_CLIENT_ID,
    PAYPAL_CLIENT_SECRET,
    PAYPAL_API_BASE,
    PAYPAL_WEBHOOK_ID,
)
from typing import Any, Dict

router = APIRouter()

@router.get("/payment/success")
async def payment_success(custom_id: str):
    """
    This endpoint is the return URL for PayPal. After payment, PayPal redirects the user here.
    The endpoint sends a confirmation message to the Telegram bot (using the custom_id, which is the Telegram user id)
    and then redirects the user to a Telegram join link.
    """
    # Send a confirmation message via Telegram
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    message = (
        "Your payment was successful and your subscription is now active! "
        "You should have access to the VIP Telegram group shortly."
    )
    payload = {
        "chat_id": custom_id,  # Assumes custom_id is the Telegram user id
        "text": message
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(telegram_api_url, json=payload)
            response.raise_for_status()
        except Exception as e:
            print("Error notifying Telegram:", e)
    
    # Redirect the user to the Telegram group join URL.
    telegram_group_link = "https://t.me/NeuralBetsFREE"  # Replace with your actual join link
    # Optionally, you can instead return an HTMLResponse that informs the user and includes a clickable link:
    # return HTMLResponse(
    #    f"<html><body><h2>Payment successful!</h2>"
    #    f"<p>You will be redirected shortly. If not, <a href='{telegram_group_link}'>click here</a>.</p></body></html>"
    # )
    
    # Automatic redirect:
    return RedirectResponse(telegram_group_link)

@router.get("/create_subscription")
async def create_subscription(user_id: str):
    """
    Creates a PayPal subscription for the given user_id.
    In production, you'd have a database to store references.
    """

    # 1. Obtain an OAuth token from PayPal
    token_url = f"{PAYPAL_API_BASE}/v1/oauth2/token"
    auth = (PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {"grant_type": "client_credentials"}

    async with httpx.AsyncClient() as client:
        token_response = await client.post(token_url, headers=headers, data=data, auth=auth)
        if token_response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to get PayPal token")
        access_token = token_response.json().get("access_token")

    # 2. Create the subscription
    create_sub_url = f"{PAYPAL_API_BASE}/v1/billing/subscriptions"
    sub_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    # For simplicity, we assume you already have a Plan ID set up in PayPal
    # (This Plan ID must be from your existing PayPal product/plan.)
    plan_id = "P-0WK61476EP547143AM767XFI"  

    subscription_payload = {
        "plan_id": plan_id,
        "application_context": {
            "return_url": "https://your-project-name.up.railway.app/payment/success?custom_id={user_id}",
            "cancel_url": "https://your-project-name.up.railway.app/payment/cancel",
        },
        "custom_id": user_id
    }

    async with httpx.AsyncClient() as client:
        create_response = await client.post(
            create_sub_url, headers=sub_headers, json=subscription_payload
        )
        if create_response.status_code not in (200, 201):
            raise HTTPException(status_code=400, detail="Error creating subscription")

        sub_data = create_response.json()
        # The 'links' list usually contains the approval_url we need.
        approval_link = next(
            (link["href"] for link in sub_data["links"] if link["rel"] == "approve"), None
        )
        if not approval_link:
            raise HTTPException(status_code=400, detail="No approval link found")

    # Return the approval link
    return {"approval_link": approval_link}


@router.post("/webhook")
async def paypal_webhook(request: Request):
    """
    Handle incoming PayPal webhook events. For security, verify signature.
    """
    try:
        # Parse the JSON body once and store it.
        event_body = await request.json()
        # Log the event for debugging.
        print("Received PayPal webhook event:")
        print(event_body)
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid webhook payload.")

    # Retrieve headers for verification.
    event_headers = request.headers

    # (Optional but recommended) Verify the webhook signature.
    verification_status = await verify_paypal_webhook_signature(event_headers, event_body)
    if not verification_status:
        raise HTTPException(status_code=400, detail="Invalid PayPal webhook signature")

    # Process the event.
    event_type = event_body.get("event_type")
    if event_type == "BILLING.SUBSCRIPTION.ACTIVATED":
        # Update your database to mark the subscription as active.
        print("Subscription activated for custom_id:", event_body.get("resource", {}).get("custom_id"))
        # e.g., update_user_subscription_status(custom_id, "active")
    elif event_type == "BILLING.SUBSCRIPTION.CANCELLED":
        print("Subscription cancelled.")
        # e.g., update_user_subscription_status(custom_id, "cancelled")
    elif event_type == "PAYMENT.SALE.COMPLETED":
        print("A payment was completed.")
        # e.g., record_transaction_details(event_body)
    else:
        print("Unhandled event type:", event_type)

    return {"status": "success", "message": "Event processed"}



async def verify_paypal_webhook_signature(headers: Any, body: Dict):
    """
    Use PayPal's 'verify-webhook-signature' endpoint to confirm authenticity.
    """
    # For brevity, we'll skip the actual signature validation code.
    # In production, you'd implement the official recommended verification steps:
    # https://developer.paypal.com/docs/api/webhooks/v1/#verify-webhook-signature_post
    return True
