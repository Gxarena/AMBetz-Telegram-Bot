# app/routes.py

from fastapi import APIRouter, Request, HTTPException
import httpx
from app.config import (
    PAYPAL_CLIENT_ID,
    PAYPAL_CLIENT_SECRET,
    PAYPAL_API_BASE,
    PAYPAL_WEBHOOK_ID,
)
from typing import Any, Dict

router = APIRouter()

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
            "return_url": "https://t.me/+9PTO3KKDwQRiMjM5",
            "cancel_url": "https://yourapp.com/payment/cancel",
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
        event = await request.json()
        # Log the event for debugging.
        print("Received PayPal webhook event:")
        print(event)
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid webhook payload.")

    # # 1. Parse the JSON body
    # event_body = await request.json()
    # event_headers = request.headers

    # # 2. (Optional but recommended) Verify the webhook signature
    # verification_status = await verify_paypal_webhook_signature(event_headers, event_body)
    # if not verification_status:
    #     raise HTTPException(status_code=400, detail="Invalid PayPal webhook signature")

    # # 3. Handle the event
    # event_type = event_body.get("event_type")

    # # For subscription events like BILLING.SUBSCRIPTION.ACTIVATED, CANCELLED, etc.
    # if event_type == "BILLING.SUBSCRIPTION.ACTIVATED":
    #     # Update your database to mark the subscription as active
    #     pass
    # elif event_type == "BILLING.SUBSCRIPTION.CANCELLED":
    #     # Mark subscription as canceled, schedule user removal from chat, etc.
    #     pass
    # elif event_type == "PAYMENT.SALE.COMPLETED":
    #     # A payment was completed; you might record transaction details
    #     pass

    return {"status": "success", "message": "Event processed"}


async def verify_paypal_webhook_signature(headers: Any, body: Dict):
    """
    Use PayPal's 'verify-webhook-signature' endpoint to confirm authenticity.
    """
    # For brevity, we'll skip the actual signature validation code.
    # In production, you'd implement the official recommended verification steps:
    # https://developer.paypal.com/docs/api/webhooks/v1/#verify-webhook-signature_post
    return True
