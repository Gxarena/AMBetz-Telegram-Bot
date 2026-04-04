import stripe
import os
import logging

from stripe_compat import metadata_get
import time
import pytz
from typing import Any, Dict, List, Optional, Tuple
from calendar import monthrange
from datetime import datetime, timedelta
from google.cloud import secretmanager

# Configure logging
logger = logging.getLogger(__name__)


class ActiveSubscriptionExistsError(ValueError):
    """Raised when Stripe already has a subscription that blocks creating a new paid checkout."""

    pass


def _subscription_period_bounds_unix(sub: Any) -> Tuple[Optional[int], Optional[int]]:
    """
    current_period_start/end on Subscription, or from first line item if Stripe omits top-level
    (common with current API shapes).

    Note: Subscription is a StripeObject (dict subclass). Use sub["items"], not sub.items —
    the latter is dict.items() and breaks line-item period reads.
    """
    try:
        cs = getattr(sub, "current_period_start", None)
        ce = getattr(sub, "current_period_end", None)
        if cs is not None and ce is not None:
            return int(cs), int(ce)
    except (TypeError, ValueError):
        pass
    items_obj = sub.get("items") if hasattr(sub, "get") else None
    data = None
    if items_obj is not None:
        data = items_obj.get("data") if hasattr(items_obj, "get") else getattr(items_obj, "data", None)
    if not data:
        return None, None
    for it0 in data:
        try:
            cs = it0.get("current_period_start") if hasattr(it0, "get") else getattr(it0, "current_period_start", None)
            ce = it0.get("current_period_end") if hasattr(it0, "get") else getattr(it0, "current_period_end", None)
            if cs is not None and ce is not None:
                return int(cs), int(ce)
        except (TypeError, ValueError, IndexError):
            continue
    return None, None


def _price_id_from_obj(obj: Any) -> Optional[str]:
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    pid = getattr(obj, "id", None)
    if pid:
        return str(pid)
    if isinstance(obj, dict):
        p = obj.get("id")
        return str(p) if p else None
    if hasattr(obj, "get"):
        p = obj.get("id")
        return str(p) if p else None
    return None


def _price_id_from_stripe_subscription(sub: Any) -> Optional[str]:
    """Stripe Price id from the first subscription item (week / 2wk / month plan)."""
    items = getattr(sub, "items", None)
    if items is None and hasattr(sub, "get"):
        items = sub.get("items")
    data = None
    if items is not None:
        data = items.get("data") if isinstance(items, dict) else getattr(items, "data", None)
    if not data:
        return None
    it0 = data[0]
    price = None
    if isinstance(it0, dict):
        price = it0.get("price")
    else:
        price = getattr(it0, "price", None)
        if price is None and hasattr(it0, "get"):
            price = it0.get("price")
    pid = _price_id_from_obj(price)
    if pid:
        return pid
    # Webhooks / older API shapes: nested `plan` on the item (often same id as `price`)
    plan = None
    if isinstance(it0, dict):
        plan = it0.get("plan")
    else:
        plan = getattr(it0, "plan", None)
        if plan is None and hasattr(it0, "get"):
            plan = it0.get("plan")
    return _price_id_from_obj(plan)


def _plan_label_from_stripe_price_id(price_id: str) -> Optional[str]:
    """
    When configured secrets don't match the live Price id, derive a label from
    Stripe Price.recurring (interval / interval_count).
    """
    if not price_id or not str(price_id).startswith("price_"):
        return None
    try:
        pr = stripe.Price.retrieve(str(price_id))
    except Exception as e:
        logger.info(
            "plan_display: Price.retrieve(%s) failed (no API fallback label): %s",
            price_id,
            e,
        )
        return None
    rec = getattr(pr, "recurring", None)
    if rec is None and hasattr(pr, "get"):
        rec = pr.get("recurring")
    if not rec:
        logger.info(
            "plan_display: Price %s has no recurring field (cannot label from interval)",
            price_id,
        )
        return None
    interval = getattr(rec, "interval", None)
    if interval is None and isinstance(rec, dict):
        interval = rec.get("interval")
    ic = getattr(rec, "interval_count", None)
    if ic is None and isinstance(rec, dict):
        ic = rec.get("interval_count")
    if not interval:
        return None
    try:
        n = int(ic) if ic is not None else 1
    except (TypeError, ValueError):
        n = 1
    n = max(1, n)
    interval = str(interval).lower()
    if interval == "week":
        return "1 week" if n == 1 else f"{n} weeks"
    if interval == "month":
        return "1 month" if n == 1 else f"{n} months"
    if interval == "year":
        return "1 year" if n == 1 else f"{n} years"
    if interval == "day":
        return "1 day" if n == 1 else f"{n} days"
    return f"{n} × {interval}"


def _sget(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    try:
        return obj[key]
    except (KeyError, TypeError):
        pass
    try:
        return getattr(obj, key)
    except AttributeError:
        return None


def _subscription_id_from_invoice(invoice: Any) -> Optional[str]:
    """
    Resolve Subscription id from an Invoice (top-level or line-item shapes).
    Stripe Checkout's first paid invoice usually has invoice.subscription set;
    some API/webhook shapes only embed the id under line items.
    """
    sid = _sget(invoice, "subscription")
    if sid:
        if isinstance(sid, str):
            return sid
        pid = _sget(sid, "id")
        return str(pid) if pid else None

    lines = _sget(invoice, "lines")
    data = _sget(lines, "data") if lines is not None else None
    if not data:
        return None

    for line in data:
        lsid = _sget(line, "subscription")
        if lsid:
            if isinstance(lsid, str):
                return lsid
            pid = _sget(lsid, "id")
            if pid:
                return str(pid)
        parent = _sget(line, "parent")
        if isinstance(parent, dict):
            sidetails = parent.get("subscription_item_details")
            if isinstance(sidetails, dict):
                sub = sidetails.get("subscription")
                if sub:
                    return str(sub)
        if isinstance(line, dict):
            pd = line.get("parent") or {}
            sidetails = pd.get("subscription_item_details") if isinstance(pd, dict) else None
            if isinstance(sidetails, dict) and sidetails.get("subscription"):
                return str(sidetails["subscription"])
    return None


def _invoice_period_bounds_unix(invoice: Any) -> Tuple[Optional[int], Optional[int]]:
    """
    Billing period from Invoice.period_* (set on subscription invoices) or from
    line-item periods. Picks the line period with the latest end time when multiple
    lines exist (avoids proration snippets shorter than the subscription cycle).
    """
    ps = _sget(invoice, "period_start")
    pe = _sget(invoice, "period_end")
    if ps is not None and pe is not None:
        try:
            ips, ipe = int(ps), int(pe)
            if ipe > ips:
                return ips, ipe
        except (TypeError, ValueError):
            pass

    lines = _sget(invoice, "lines")
    data = _sget(lines, "data") if lines is not None else None
    if not data:
        return None, None

    best_end: Optional[int] = None
    best_start: Optional[int] = None
    for line in data:
        period = _sget(line, "period")
        if not period:
            continue
        ls = _sget(period, "start")
        le = _sget(period, "end")
        if ls is None or le is None:
            continue
        try:
            ls_i, le_i = int(ls), int(le)
        except (TypeError, ValueError):
            continue
        if le_i <= ls_i:
            continue
        if best_end is None or le_i > best_end:
            best_end = le_i
            best_start = ls_i

    if best_start is not None and best_end is not None:
        return best_start, best_end
    return None, None


def _add_calendar_months(dt: datetime, months: int) -> datetime:
    if months <= 0:
        return dt
    y, m, d = dt.year, dt.month, dt.day
    total_m = m - 1 + months
    y += total_m // 12
    m = total_m % 12 + 1
    last = monthrange(y, m)[1]
    d = min(d, last)
    return dt.replace(year=y, month=m, day=d)


def _expiry_from_recurring_start(
    start: datetime, interval: str, interval_count: int
) -> Optional[datetime]:
    n = max(1, interval_count)
    iv = (interval or "").lower()
    if iv == "day":
        return start + timedelta(days=n)
    if iv == "week":
        return start + timedelta(weeks=n)
    if iv == "month":
        return _add_calendar_months(start, n)
    if iv == "year":
        return _add_calendar_months(start, 12 * n)
    logger.warning(
        "Unsupported Stripe recurring interval %r (interval_count=%s); cannot derive expiry from price",
        interval,
        interval_count,
    )
    return None


def expiry_from_recurring_price_id(
    start_date: datetime, price_id: Optional[str]
) -> Optional[datetime]:
    """Compute next expiry from a Price id using its recurring interval (subscription prices)."""
    if not price_id or not str(price_id).startswith("price_"):
        return None
    try:
        pr = stripe.Price.retrieve(str(price_id))
    except Exception as exc:
        logger.warning("expiry_from_recurring_price_id: Price.retrieve(%s) failed: %s", price_id, exc)
        return None
    rec = _sget(pr, "recurring")
    if not rec:
        return None
    interval = _sget(rec, "interval")
    ic_raw = _sget(rec, "interval_count")
    try:
        ic = int(ic_raw) if ic_raw is not None else 1
    except (TypeError, ValueError):
        ic = 1
    if not interval:
        return None
    return _expiry_from_recurring_start(start_date, str(interval), ic)


def _price_ids_from_invoice_line(line: Any) -> List[str]:
    """Collect price ids from an invoice line (classic and newer API shapes)."""
    out: List[str] = []
    p = _sget(line, "price")
    if isinstance(p, str) and p.startswith("price_"):
        out.append(p)
    else:
        pid = _price_id_from_obj(p)
        if pid:
            out.append(pid)
    plan = _sget(line, "plan")
    if plan:
        pid = _price_id_from_obj(plan)
        if pid:
            out.append(pid)
    pricing = _sget(line, "pricing")
    if pricing:
        pd = _sget(pricing, "price_details")
        if pd:
            raw = _sget(pd, "price")
            if isinstance(raw, str) and raw.startswith("price_"):
                out.append(raw)
            else:
                pid = _price_id_from_obj(raw)
                if pid:
                    out.append(pid)
    seen: set = set()
    deduped: List[str] = []
    for pid in out:
        if pid not in seen:
            seen.add(pid)
            deduped.append(pid)
    return deduped


def expiry_from_invoice_recurring_prices(
    invoice: Any, start_date: datetime
) -> Optional[datetime]:
    """
    Derive subscription length from recurring Price objects referenced on invoice lines.
    Tries each distinct price id until one has recurring.interval set.
    """
    lines = _sget(invoice, "lines")
    data = _sget(lines, "data") if lines else None
    if not data:
        return None
    for line in data:
        for pid in _price_ids_from_invoice_line(line):
            exp = expiry_from_recurring_price_id(start_date, pid)
            if exp is not None:
                return exp
    return None


def subscription_fallback_expiry(
    subscription: Any,
    start_date: datetime,
    *,
    is_trial: bool = False,
    invoice: Any = None,
) -> Optional[datetime]:
    """
    When current_period_start/end are missing: use first subscription item's price.recurring
    (via expanded object or Price.retrieve). Trial → +3 days.
    If still unknown, optionally resolve from invoice line prices.
    Returns None if expiry cannot be derived (caller should avoid guessing 30 days).
    """
    if is_trial:
        return start_date + timedelta(days=3)

    items_obj = subscription.get("items") if hasattr(subscription, "get") else None
    data = None
    if items_obj is not None:
        data = (
            items_obj.get("data")
            if hasattr(items_obj, "get")
            else getattr(items_obj, "data", None)
        )

    def _from_invoice() -> Optional[datetime]:
        if invoice is None:
            return None
        return expiry_from_invoice_recurring_prices(invoice, start_date)

    if not data:
        return _from_invoice()

    price = _sget(data[0], "price")
    if isinstance(price, str):
        price_id_str = str(price)
        if not price_id_str.startswith("price_"):
            return _from_invoice()
        try:
            price = stripe.Price.retrieve(price_id_str)
        except Exception as exc:
            logger.warning(
                "subscription_fallback_expiry: Price.retrieve(%s) failed: %s",
                price_id_str,
                exc,
            )
            return _from_invoice()

    rec = _sget(price, "recurring")
    if not rec:
        return _from_invoice()

    interval = _sget(rec, "interval")
    ic_raw = _sget(rec, "interval_count")
    try:
        ic = int(ic_raw) if ic_raw is not None else 1
    except (TypeError, ValueError):
        ic = 1

    if not interval:
        return _from_invoice()

    exp = _expiry_from_recurring_start(start_date, str(interval), ic)
    if exp is not None:
        return exp
    return _from_invoice()


def _list_subscriptions_paginated(customer_id: str, status: str) -> List[Any]:
    """All subscriptions for customer with given status."""
    out: List[Any] = []
    params: Dict[str, Any] = {"customer": customer_id, "status": status, "limit": 100}
    while True:
        page = stripe.Subscription.list(**params)
        batch = page.data or []
        out.extend(batch)
        if not getattr(page, "has_more", False) or not batch:
            break
        params["starting_after"] = batch[-1].id
    return out

class GCPStripeService:
    def __init__(self, project_id: str = None):
        """Initialize Stripe service with GCP Secret Manager integration"""
        self.project_id = project_id or os.getenv('GOOGLE_CLOUD_PROJECT')
        if not self.project_id:
            raise ValueError("GOOGLE_CLOUD_PROJECT environment variable is required")
        
        # Initialize Secret Manager client
        self.secret_client = secretmanager.SecretManagerServiceClient()
        
        # Get Stripe credentials from Secret Manager
        self.publishable_key = self._get_secret("stripe-publishable-key")
        self.secret_key = self._get_secret("stripe-secret-key")
        self.webhook_secret = self._get_secret("stripe-webhook-secret")
        self.price_id = self._get_secret("stripe-price-id")
        # Optional shorter billing periods (same Stripe product, different recurring prices)
        self.price_id_week = (self._get_secret("stripe-price-id-week") or "").strip()
        self.price_id_2week = (self._get_secret("stripe-price-id-2week") or "").strip()
        
        # Check if Stripe is configured
        self.is_configured = bool(self.secret_key)
        
        if self.is_configured:
            stripe.api_key = self.secret_key
            _plans = self.get_subscription_plan_options()
            logger.info(
                "GCP Stripe service initialized; subscription plan keys for checkout: %s (%d plan(s))",
                [p["key"] for p in _plans],
                len(_plans),
            )
        else:
            logger.warning("Stripe not configured - payment features will be disabled")

    def get_subscription_plan_options(self) -> List[Dict[str, str]]:
        """
        Plans shown in the bot after the user taps Subscribe.
        Keys: week, 2week, month — must match callback_data subscribe_plan:<key>.
        """
        plans: List[Dict[str, str]] = []
        if self.price_id_week:
            plans.append(
                {"key": "week", "price_id": self.price_id_week, "label": "1 week — $35"}
            )
        if self.price_id_2week:
            plans.append(
                {"key": "2week", "price_id": self.price_id_2week, "label": "2 weeks — $50"}
            )
        if self.price_id:
            plans.append(
                {"key": "month", "price_id": self.price_id, "label": "1 month — $75"}
            )
        return plans

    def price_id_for_plan_key(self, plan_key: str) -> Optional[str]:
        for p in self.get_subscription_plan_options():
            if p["key"] == plan_key:
                return p["price_id"]
        return None

    def cancel_terminal_and_incomplete_subscriptions(self, customer_id: str) -> int:
        """
        Before starting a new Checkout session: remove subscriptions that are not 'active' or 'trialing'
        but still block a clean billing story (past_due, unpaid, incomplete). Prevents stacking a
        second subscription on the same customer while an old one is failed/abandoned.
        """
        cancelled = 0
        for status in ("past_due", "unpaid", "incomplete"):
            for sub in _list_subscriptions_paginated(customer_id, status):
                try:
                    stripe.Subscription.cancel(sub.id)
                    cancelled += 1
                    logger.info(
                        "Cancelled %s subscription %s before new checkout for customer %s",
                        status,
                        sub.id,
                        customer_id,
                    )
                except stripe.StripeError as exc:
                    logger.error(
                        "Failed to cancel %s subscription %s: %s", status, sub.id, exc
                    )
        return cancelled

    def cancel_other_subscriptions_except(self, customer_id: str, keep_subscription_id: str) -> int:
        """
        After a successful subscription Checkout: cancel every other subscription on this customer
        so only the newly paid subscription remains (VIP is single-product).
        """
        cancelled = 0
        for status in ("active", "trialing", "past_due", "unpaid", "incomplete"):
            for sub in _list_subscriptions_paginated(customer_id, status):
                if sub.id == keep_subscription_id:
                    continue
                try:
                    stripe.Subscription.cancel(sub.id)
                    cancelled += 1
                    logger.info(
                        "Cancelled extra subscription %s (status=%s) for customer %s; keeping %s",
                        sub.id,
                        status,
                        customer_id,
                        keep_subscription_id,
                    )
                except stripe.StripeError as exc:
                    logger.error("Failed to cancel subscription %s: %s", sub.id, exc)
        return cancelled

    def _ensure_paid_subscription_checkout_allowed(self, customer_id: str) -> None:
        """
        Paid plan checkout must not run while the customer is still in trialing — otherwise Stripe
        would create a second subscription (bot UI should also block this).
        Active subscriptions are already rejected inside get_or_create_customer.
        """
        now_ts = int(time.time())
        trialing = stripe.Subscription.list(customer=customer_id, status="trialing")
        for sub in trialing.data or []:
            _, period_end = _subscription_period_bounds_unix(sub)
            if period_end is None or period_end > now_ts:
                raise ActiveSubscriptionExistsError(
                    "You already have a trial or trialing subscription. Wait for it to convert, "
                    "or cancel it in the customer portal before buying a plan."
                )

    def expire_open_checkout_sessions_for_customer(self, customer_id: str) -> int:
        """
        Invalidate any other open Checkout sessions for this customer so old links cannot be paid.
        Safe to call after a subscription successfully starts (the completed session is not 'open').
        """
        if not customer_id or not self.is_configured:
            return 0
        n = 0
        try:
            sessions = stripe.checkout.Session.list(
                customer=customer_id, status="open", limit=100
            )
            for s in sessions.auto_paging_iter():
                try:
                    stripe.checkout.Session.expire(s.id)
                    n += 1
                    logger.info(
                        "Expired open checkout session %s for customer %s", s.id, customer_id
                    )
                except stripe.InvalidRequestError as exc:
                    err = str(exc).lower()
                    if (
                        "not in open status" in err
                        or "already completed" in err
                        or "cannot expire" in err
                    ):
                        continue
                    logger.warning("Could not expire session %s: %s", s.id, exc)
                except stripe.StripeError as exc:
                    logger.warning("Could not expire session %s: %s", s.id, exc)
        except stripe.StripeError as exc:
            logger.error("expire_open_checkout_sessions_for_customer list failed: %s", exc)
        return n

    def revert_duplicate_active_checkout(self, session) -> None:
        """
        Cancel the subscription created by this Checkout session only (no refund).

        Used when the user already had an active VIP but completed another Checkout anyway.
        We do not refund: duplicate checkouts in that situation are treated like policy elsewhere
        (final sale / no refunds). Must run *before* handle_successful_payment — that path calls
        cancel_other_subscriptions_except and would cancel their legitimate subscription first.
        """
        sub_id = getattr(session, "subscription", None)
        if not sub_id and isinstance(session, dict):
            sub_id = session.get("subscription")
        if not sub_id:
            logger.warning(
                "revert_duplicate_active_checkout: no subscription on session %s",
                getattr(session, "id", "?"),
            )
            return
        sid = getattr(session, "id", None) or (session.get("id") if isinstance(session, dict) else None)
        try:
            sub = stripe.Subscription.retrieve(sub_id)
            if getattr(sub, "status", None) in ("canceled", "incomplete_expired"):
                logger.info(
                    "Duplicate revert: subscription %s already %s (skip cancel)",
                    sub_id,
                    sub.status,
                )
            else:
                stripe.Subscription.cancel(sub_id)
                logger.info(
                    "Cancelled duplicate subscription %s from checkout session %s",
                    sub_id,
                    sid,
                )
        except stripe.StripeError as exc:
            logger.error("Failed to cancel duplicate subscription %s: %s", sub_id, exc)

    def _env_lookup_for_secret_id(self, secret_name: str) -> str:
        """Map Secret Manager id (e.g. stripe-secret-key-test) to env vars."""
        key = secret_name.upper().replace("-", "_")
        val = os.getenv(key)
        if val:
            return val
        if os.getenv("DEVELOPMENT_MODE", "false").lower() == "true" and secret_name.endswith("-test"):
            base = secret_name[: -len("-test")]
            val2 = os.getenv(base.upper().replace("-", "_"))
            if val2:
                return val2
        return ""

    def _get_secret(self, secret_name: str) -> str:
        """Get secret from GCP Secret Manager, with optional .env preference for Stripe (local dev)."""
        original_name = secret_name
        if os.getenv("DEVELOPMENT_MODE", "false").lower() == "true":
            secret_name = f"{secret_name}-test"

        # GCP may still hold sk_test_* while .env has sk_live_* — Secret Manager wins unless:
        if (
            os.getenv("STRIPE_PREFER_DOTENV", "").lower() in ("1", "true", "yes")
            and original_name.startswith("stripe-")
        ):
            v = self._env_lookup_for_secret_id(secret_name)
            if v:
                logger.info(
                    "STRIPE_PREFER_DOTENV: using Stripe value from environment for %s",
                    original_name,
                )
                return v

        try:
            name = f"projects/{self.project_id}/secrets/{secret_name}/versions/latest"
            response = self.secret_client.access_secret_version(request={"name": name})
            return response.payload.data.decode("UTF-8")
        except Exception as e:
            logger.error(f"Error accessing secret {secret_name}: {e}")
            return self._env_lookup_for_secret_id(secret_name) or ""

    def create_payment_link(self, telegram_id: int, telegram_username: str = None, price_id: str = None) -> str:
        """Create a Stripe payment link for a user"""
        if not self.is_configured:
            raise ValueError("Stripe is not configured")
        pid = price_id or self.price_id
        if not pid:
            raise ValueError("No Stripe price ID configured")
            
        try:
            # Create or retrieve customer
            customer = self.get_or_create_customer(telegram_id, telegram_username)
            self.cancel_terminal_and_incomplete_subscriptions(customer.id)

            # Create payment link
            payment_link = stripe.PaymentLink.create(
                line_items=[
                    {
                        "price": pid,
                        "quantity": 1,
                    },
                ],
                metadata={
                    "telegram_id": str(telegram_id),
                    "telegram_username": telegram_username or "",
                    "source": "gcp-bot"
                }
            )
            
            logger.info(f"Payment link created for user {telegram_id}")
            return payment_link.url
            
        except Exception as e:
            logger.error(f"Error creating payment link: {e}")
            raise

    def create_subscription_checkout(self, telegram_id: int, telegram_username: str = None, price_id: str = None) -> str:
        """Create a Stripe checkout session for recurring subscription"""
        if not self.is_configured:
            raise ValueError("Stripe is not configured")
        pid = price_id or self.price_id
        if not pid:
            raise ValueError("No Stripe price ID configured")
            
        try:
            # Sanitize username to remove problematic Unicode characters
            sanitized_username = self._sanitize_string(telegram_username) if telegram_username else ""
            
            # Create or retrieve customer
            customer = self.get_or_create_customer(telegram_id, sanitized_username)
            self.cancel_terminal_and_incomplete_subscriptions(customer.id)
            self._ensure_paid_subscription_checkout_allowed(customer.id)

            # Create checkout session for subscription
            checkout_session = stripe.checkout.Session.create(
                customer=customer.id,
                payment_method_types=['card'],
                line_items=[
                    {
                        'price': pid,
                        'quantity': 1,
                    },
                ],
                mode='subscription',  # This makes it recurring!
                success_url=f'https://t.me/AMBETZBot?start=success',
                cancel_url=f'https://t.me/AMBETZBot?start=cancelled',
                metadata={
                    "telegram_id": str(telegram_id),
                    "telegram_username": sanitized_username,
                    "source": "gcp-bot"
                }
            )
            
            logger.info(f"Subscription checkout session created for user {telegram_id}")
            return checkout_session.url
            
        except Exception as e:
            logger.error(f"Error creating subscription checkout: {e}")
            raise
    
    def create_trial_subscription_checkout(self, telegram_id: int, telegram_username: str = None, trial_days: int = 3) -> str:
        """Create a Stripe checkout session for subscription with free trial period"""
        if not self.is_configured:
            raise ValueError("Stripe is not configured")
            
        try:
            # Sanitize username to remove problematic Unicode characters
            sanitized_username = self._sanitize_string(telegram_username) if telegram_username else ""
            
            # Create or retrieve customer
            customer = self.get_or_create_customer(telegram_id, sanitized_username)
            self.cancel_terminal_and_incomplete_subscriptions(customer.id)

            # Create checkout session for subscription with trial period
            checkout_session = stripe.checkout.Session.create(
                customer=customer.id,
                payment_method_types=['card'],
                line_items=[
                    {
                        'price': self.price_id,
                        'quantity': 1,
                    },
                ],
                mode='subscription',
                subscription_data={
                    'trial_period_days': trial_days,
                    'metadata': {
                        "telegram_id": str(telegram_id),
                        "telegram_username": sanitized_username,
                        "source": "gcp-bot",
                        "is_trial": "true"
                    }
                },
                success_url=f'https://t.me/AMBETZBot?start=success',
                cancel_url=f'https://t.me/AMBETZBot?start=cancelled',
                metadata={
                    "telegram_id": str(telegram_id),
                    "telegram_username": sanitized_username,
                    "source": "gcp-bot",
                    "is_trial": "true"
                }
            )
            
            logger.info(f"Trial subscription checkout session created for user {telegram_id} with {trial_days} day trial")
            return checkout_session.url
            
        except Exception as e:
            logger.error(f"Error creating trial subscription checkout: {e}")
            raise
    
    def get_or_create_customer(self, telegram_id: int, telegram_username: str = None) -> stripe.Customer:
        """Get existing customer or create new one"""
        if not self.is_configured:
            raise ValueError("Stripe is not configured")
            
        try:
            # Search for existing customer by telegram_id
            customers = stripe.Customer.search(
                query=f"metadata['telegram_id']:'{telegram_id}'"
            )
            
            if customers.data:
                # Check if customer has active subscriptions (excluding trials)
                customer_id = customers.data[0].id
                active_subscriptions = stripe.Subscription.list(customer=customer_id, status='active')
                trialing_subscriptions = stripe.Subscription.list(customer=customer_id, status='trialing')
                
                # Check for active (non-trial) subscriptions that are not already ended
                # Allow resubscribe if all "active" subs have current_period_end in the past
                # (Stripe can still list them as active briefly before subscription.deleted fires)
                now_ts = int(time.time())
                truly_active = []
                for sub in (active_subscriptions.data or []):
                    _, period_end = _subscription_period_bounds_unix(sub)
                    if period_end is not None and period_end < now_ts:
                        # Period already ended - treat as over (Stripe may not have sent deleted yet)
                        continue
                    truly_active.append(sub)
                if truly_active:
                    # Customer has a real active subscription - block duplicate
                    logger.warning(f"Customer {customer_id} already has active subscription, rejecting new subscription attempt")
                    raise ActiveSubscriptionExistsError(
                        "You already have an active paid subscription. Use /status to see when it renews."
                    )
                
                # Allow trialing subscriptions (user might be starting a new trial or converting trial to paid)
                # The bot logic will handle preventing duplicate trials
                if trialing_subscriptions.data:
                    logger.info(f"Customer {customer_id} has trialing subscription, allowing access")
                
                return customers.data[0]
            
            # Create new customer if not found
            customer = stripe.Customer.create(
                metadata={
                    "telegram_id": str(telegram_id),
                    "telegram_username": telegram_username or "",
                    "source": "gcp-bot"
                }
            )
            
            logger.info(f"Customer created for telegram user {telegram_id}")
            return customer
            
        except Exception as e:
            logger.error(f"Error handling customer: {e}")
            raise
    
    def cancel_active_subscriptions(self, telegram_id: int) -> bool:
        """Cancel all active and trialing subscriptions for a customer (e.g. orphan subs after sync)."""
        if not self.is_configured:
            raise ValueError("Stripe is not configured")
        
        try:
            # Find customer by telegram_id
            customers = stripe.Customer.search(
                query=f"metadata['telegram_id']:'{telegram_id}'"
            )
            
            if not customers.data:
                logger.info(f"No Stripe customer found for telegram_id {telegram_id}")
                return False
            
            customer_id = customers.data[0].id
            cancelled_count = 0
            
            # Cancel all active subscriptions
            active_subscriptions = stripe.Subscription.list(customer=customer_id, status='active')
            for sub in active_subscriptions.data:
                try:
                    stripe.Subscription.cancel(sub.id)
                    logger.info(f"Cancelled active subscription {sub.id} for customer {customer_id}")
                    cancelled_count += 1
                except Exception as e:
                    logger.error(f"Error cancelling subscription {sub.id}: {e}")
            
            # Cancel all trialing subscriptions
            trialing_subscriptions = stripe.Subscription.list(customer=customer_id, status='trialing')
            for sub in trialing_subscriptions.data:
                try:
                    stripe.Subscription.cancel(sub.id)
                    logger.info(f"Cancelled trialing subscription {sub.id} for customer {customer_id}")
                    cancelled_count += 1
                except Exception as e:
                    logger.error(f"Error cancelling trialing subscription {sub.id}: {e}")
            
            logger.info(f"Cancelled {cancelled_count} subscription(s) for telegram_id {telegram_id}")
            return cancelled_count > 0
            
        except Exception as e:
            logger.error(f"Error cancelling subscriptions for telegram_id {telegram_id}: {e}")
            raise

    def _pick_canonical_subscription_id(self, customer_id: str) -> Tuple[Optional[str], str]:
        """If multiple active/trialing subs, pick the one with the latest current_period_end."""
        candidates: List[str] = []
        for st in ("active", "trialing"):
            for sub in _list_subscriptions_paginated(customer_id, st):
                candidates.append(sub.id)
        if not candidates:
            return None, "no_active_or_trialing"
        if len(candidates) == 1:
            return candidates[0], "single_match"
        best_id: Optional[str] = None
        best_end = 0
        for sid in candidates:
            try:
                full = stripe.Subscription.retrieve(
                    sid, expand=["items.data", "items.data.price"]
                )
                _, pe = _subscription_period_bounds_unix(full)
                if pe is not None and pe >= best_end:
                    best_end = pe
                    best_id = sid
            except Exception as exc:
                logger.warning("Could not retrieve subscription %s for pick: %s", sid, exc)
        if best_id:
            return best_id, f"picked_latest_period_end_among_{len(candidates)}"
        return candidates[0], f"fallback_first_of_{len(candidates)}"

    def try_refresh_firestore_mirror_from_stripe(self, telegram_id: int, firestore_service: Any) -> bool:
        """
        Stripe is the billing source of truth. If Stripe shows active/trialing with
        current_period_end in the future, update Firestore to match (merge-update).

        Returns True if Firestore was updated to active with Stripe's period bounds.
        """
        if not self.is_configured:
            return False
        try:
            existing = firestore_service.get_subscription(telegram_id)
            customer_id = (existing or {}).get("stripe_customer_id")
            sub_id_from_doc = (existing or {}).get("stripe_subscription_id")

            sub_obj = None
            resolved_customer_id = customer_id

            if sub_id_from_doc:
                try:
                    sub_obj = stripe.Subscription.retrieve(
                        sub_id_from_doc,
                        expand=["items.data", "items.data.price"],
                    )
                except stripe.InvalidRequestError:
                    sub_obj = None

            if sub_obj is None or getattr(sub_obj, "status", None) not in ("active", "trialing"):
                if not resolved_customer_id:
                    customers = stripe.Customer.search(
                        query=f"metadata['telegram_id']:'{telegram_id}'",
                        limit=5,
                    )
                    if not customers.data:
                        return False
                    resolved_customer_id = customers.data[0].id
                sub_pick, _ = self._pick_canonical_subscription_id(resolved_customer_id)
                if not sub_pick:
                    return False
                sub_obj = stripe.Subscription.retrieve(
                    sub_pick, expand=["items.data", "items.data.price"]
                )

            st = getattr(sub_obj, "status", None)
            if st not in ("active", "trialing"):
                return False

            cps_u, cpe_u = _subscription_period_bounds_unix(sub_obj)
            if cpe_u is None:
                return False
            now_ts = int(time.time())
            if cpe_u < now_ts:
                return False

            if cps_u is None:
                cps_u = cpe_u
            start_dt = datetime.fromtimestamp(cps_u, tz=pytz.UTC)
            end_dt = datetime.fromtimestamp(cpe_u, tz=pytz.UTC)

            cust_final = resolved_customer_id or getattr(sub_obj, "customer", None)
            if isinstance(cust_final, dict):
                cust_final = cust_final.get("id")
            if not cust_final:
                return False

            price_id = _price_id_from_stripe_subscription(sub_obj)
            ok = firestore_service.sync_subscription_active_from_stripe(
                telegram_id=telegram_id,
                start_date=start_dt,
                expiry_date=end_dt,
                stripe_customer_id=str(cust_final),
                stripe_subscription_id=sub_obj.id,
                stripe_price_id=price_id,
            )
            return bool(ok)
        except Exception as e:
            logger.warning(
                "try_refresh_firestore_mirror_from_stripe failed for telegram_id=%s: %s",
                telegram_id,
                e,
            )
            return False

    def _resolve_stripe_price_id_for_plan_display(
        self,
        doc: Dict[str, Any],
        telegram_id: Optional[int],
        log_pfx: str,
    ) -> Optional[str]:
        """
        When Firestore omits stripe_price_id (and often stripe_subscription_id), resolve the
        active Price id the same way try_refresh does: subscription id → customer →
        Customer.search(telegram_id).
        """
        if not self.is_configured:
            logger.info("%s skip price resolve: Stripe not configured", log_pfx)
            return None

        sub_id = doc.get("stripe_subscription_id")
        if sub_id:
            try:
                sub = stripe.Subscription.retrieve(
                    str(sub_id),
                    expand=["items.data.price"],
                )
                pid = _price_id_from_stripe_subscription(sub)
                logger.info(
                    "%s resolved via stripe_subscription_id=%r -> price_id=%r",
                    log_pfx,
                    sub_id,
                    pid,
                )
                return pid
            except Exception as e:
                logger.warning(
                    "%s Subscription.retrieve(%r) failed: %s",
                    log_pfx,
                    sub_id,
                    e,
                    exc_info=True,
                )

        cust_raw = doc.get("stripe_customer_id")
        if isinstance(cust_raw, dict):
            cust_raw = cust_raw.get("id")
        cust_id = str(cust_raw).strip() if cust_raw else None

        if cust_id:
            try:
                sub_pick, reason = self._pick_canonical_subscription_id(cust_id)
                logger.info(
                    "%s canonical sub for stripe_customer_id=%r -> %r (%s)",
                    log_pfx,
                    cust_id,
                    sub_pick,
                    reason,
                )
                if sub_pick:
                    sub = stripe.Subscription.retrieve(
                        sub_pick, expand=["items.data.price"]
                    )
                    pid = _price_id_from_stripe_subscription(sub)
                    logger.info(
                        "%s resolved via customer -> subscription -> price_id=%r",
                        log_pfx,
                        pid,
                    )
                    return pid
            except Exception as e:
                logger.warning(
                    "%s resolve via stripe_customer_id failed: %s",
                    log_pfx,
                    e,
                    exc_info=True,
                )

        if telegram_id is not None:
            try:
                customers = stripe.Customer.search(
                    query=f"metadata['telegram_id']:'{telegram_id}'",
                    limit=5,
                )
                if not customers.data:
                    logger.info(
                        "%s Customer.search(telegram_id) found no Stripe customer",
                        log_pfx,
                    )
                    return None
                cid = customers.data[0].id
                sub_pick, reason = self._pick_canonical_subscription_id(cid)
                logger.info(
                    "%s Customer.search -> %r; canonical sub=%r (%s)",
                    log_pfx,
                    cid,
                    sub_pick,
                    reason,
                )
                if sub_pick:
                    sub = stripe.Subscription.retrieve(
                        sub_pick, expand=["items.data.price"]
                    )
                    pid = _price_id_from_stripe_subscription(sub)
                    logger.info(
                        "%s resolved via Customer.search path -> price_id=%r",
                        log_pfx,
                        pid,
                    )
                    return pid
            except Exception as e:
                logger.warning(
                    "%s Customer.search resolve failed: %s",
                    log_pfx,
                    e,
                    exc_info=True,
                )

        return None

    def plan_display_for_subscription_doc(
        self, doc: Optional[Dict[str, Any]], telegram_id: Optional[int] = None
    ) -> str:
        """
        Human-readable plan for /status: 1 week, 2 weeks, 1 month, free trial, etc.
        Uses stored stripe_price_id when present; otherwise resolves from Stripe if possible.
        """
        log_pfx = (
            f"[plan_display telegram_id={telegram_id}]"
            if telegram_id is not None
            else "[plan_display]"
        )

        if not doc:
            logger.info("%s no doc -> em dash", log_pfx)
            return "—"
        meta = doc.get("metadata") or {}
        if isinstance(meta, dict) and meta.get("is_trial"):
            logger.info("%s metadata.is_trial -> Free trial", log_pfx)
            return "Free trial"
        st = (doc.get("subscription_type") or "").lower()
        if st == "trial":
            logger.info("%s subscription_type trial -> Free trial", log_pfx)
            return "Free trial"

        pid = doc.get("stripe_price_id")
        sub_id = doc.get("stripe_subscription_id")
        cust_log = doc.get("stripe_customer_id")
        logger.info(
            "%s inputs: stripe_price_id=%r stripe_subscription_id=%r stripe_customer_id=%r "
            "subscription_type=%r status=%r stripe_service.is_configured=%s",
            log_pfx,
            pid,
            sub_id,
            cust_log,
            st,
            doc.get("status"),
            self.is_configured,
        )

        if not pid:
            pid = self._resolve_stripe_price_id_for_plan_display(doc, telegram_id, log_pfx)

        if pid:
            opts = self.get_subscription_plan_options()
            configured = [(p.get("key"), p.get("price_id")) for p in opts]
            logger.info("%s matching price_id=%r against configured=%s", log_pfx, pid, configured)
            for p in opts:
                if p["price_id"] == pid:
                    key = p["key"]
                    if key == "week":
                        logger.info("%s secret match -> 1 week", log_pfx)
                        return "1 week"
                    if key == "2week":
                        logger.info("%s secret match -> 2 weeks", log_pfx)
                        return "2 weeks"
                    if key == "month":
                        logger.info("%s secret match -> 1 month", log_pfx)
                        return "1 month"
            api_label = _plan_label_from_stripe_price_id(str(pid))
            logger.info(
                "%s no secret match; Stripe Price API label=%r for price_id=%r",
                log_pfx,
                api_label,
                pid,
            )
            if api_label:
                return api_label
            logger.info("%s fallback label -> Premium (recurring)", log_pfx)
            return "Premium (recurring)"

        logger.info(
            "%s no price_id after Firestore + Stripe resolution; subscription_type=%r -> generic label path",
            log_pfx,
            st,
        )
        if st == "premium":
            logger.info("%s -> Premium (no stripe price id resolved)", log_pfx)
            return "Premium"
        if st == "test":
            logger.info("%s -> Test", log_pfx)
            return "Test"
        out = st.title() if st else "Unknown"
        logger.info("%s -> %r", log_pfx, out)
        return out
    
    def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        """Verify webhook signature from Stripe"""
        if not self.is_configured:
            return False
            
        try:
            stripe.Webhook.construct_event(
                payload, signature, self.webhook_secret
            )
            return True
        except ValueError:
            logger.error("Invalid payload")
            return False
        except stripe.error.SignatureVerificationError:
            logger.error("Invalid signature")
            return False
    
    def handle_successful_payment(self, session_data) -> Dict[str, Any]:
        """Handle successful payment and return subscription info"""
        try:
            # Extract metadata - handle both dict and Stripe object
            logger.info(f"Session data type: {type(session_data)}")
            logger.info(f"Session data attributes: {dir(session_data)}")
            
            # Try multiple ways to get metadata
            metadata = None
            if hasattr(session_data, 'metadata'):
                metadata = session_data.metadata
                logger.info(f"Metadata from attribute: {metadata}")
            elif hasattr(session_data, 'get'):
                metadata = session_data.get("metadata", {})
                logger.info(f"Metadata from get(): {metadata}")
            
            # Get telegram_id from metadata
            logger.info(f"Metadata type: {type(metadata)}")
            logger.info(f"Metadata content: {metadata}")
            
            telegram_id = None
            try:
                telegram_id = metadata_get(metadata, "telegram_id")
                logger.info(f"Telegram ID from metadata: {telegram_id}")
            except Exception as e:
                logger.error(f"Error accessing telegram_id from metadata: {e}")
                logger.error(f"Metadata type: {type(metadata)}")
                logger.error(f"Metadata content: {metadata}")
                telegram_id = None
            
            # FALLBACK: If no telegram_id in session metadata, try to get it from the customer
            if not telegram_id:
                logger.warning("No telegram_id in session metadata, attempting fallback methods...")
                
                # Get customer ID from session
                customer_id = None
                if hasattr(session_data, 'customer'):
                    customer_id = session_data.customer
                elif hasattr(session_data, 'get'):
                    customer_id = session_data.get("customer")
                
                if customer_id:
                    try:
                        # Retrieve customer from Stripe to get metadata
                        customer = stripe.Customer.retrieve(customer_id)
                        telegram_id = metadata_get(customer.metadata, "telegram_id")
                        logger.info(f"Retrieved telegram_id from customer metadata: {telegram_id}")
                    except Exception as e:
                        logger.error(f"Error retrieving customer {customer_id}: {e}")
                
                # If still no telegram_id, try to find it by email in Firestore
                if not telegram_id and customer_id:
                    try:
                        customer = stripe.Customer.retrieve(customer_id)
                        customer_email = customer.email
                        if customer_email:
                            logger.info(f"Attempting to find telegram_id by email: {customer_email}")
                            
                            # Import FirestoreService here to avoid circular imports
                            from firestore_service import FirestoreService
                            project_id = os.getenv('GOOGLE_CLOUD_PROJECT')
                            firestore_service = FirestoreService(project_id)
                            
                            # Try to find user by email
                            user_data = firestore_service.get_user_by_email(customer_email)
                            if user_data and user_data.get('telegram_id'):
                                telegram_id = user_data['telegram_id']
                                logger.info(f"Found telegram_id by email lookup: {telegram_id}")
                                
                                # Update the Stripe customer with the found telegram_id
                                try:
                                    stripe.Customer.modify(
                                        customer_id,
                                        metadata={
                                            'telegram_id': str(telegram_id),
                                            'telegram_username': user_data.get('username', ''),
                                            'source': 'gcp-bot',
                                            'linked_by_email': 'true'
                                        }
                                    )
                                    logger.info(f"Updated Stripe customer {customer_id} with telegram_id {telegram_id}")
                                except Exception as e:
                                    logger.error(f"Failed to update Stripe customer metadata: {e}")
                            else:
                                logger.warning(f"Customer {customer_id} ({customer_email}) has no telegram_id - manual intervention required")
                    except Exception as e:
                        logger.error(f"Error getting customer email: {e}")
            
            if not telegram_id:
                logger.error("No telegram_id found in payment metadata or customer data")
                logger.error(f"Session metadata: {metadata}")
                logger.error("This payment cannot be processed - customer needs manual linking")
                return None

            # Get session data - handle both dict and Stripe object (needed before subscription cleanup)
            if hasattr(session_data, 'customer'):
                customer_id = session_data.customer
            else:
                customer_id = session_data.get("customer")

            subscription_object = None
            is_trial = False

            # For subscriptions, get the actual subscription period from Stripe
            if hasattr(session_data, 'subscription') and session_data.subscription:
                subscription_object = stripe.Subscription.retrieve(
                    session_data.subscription,
                    expand=["items.data", "items.data.price"],
                )

                meta_is_trial = metadata_get(metadata, "is_trial") == "true"
                is_trial = (
                    subscription_object.status == "trialing" or meta_is_trial
                )

                cps, cpe = _subscription_period_bounds_unix(subscription_object)
                if cps is not None and cpe is not None:
                    start_date = datetime.fromtimestamp(cps, tz=pytz.UTC)
                    expiry_date = datetime.fromtimestamp(cpe, tz=pytz.UTC)
                elif is_trial and getattr(subscription_object, "trial_start", None) and getattr(
                    subscription_object, "trial_end", None
                ):
                    logger.warning(
                        "Subscription %s missing item period bounds, using trial_start/trial_end",
                        subscription_object.id,
                    )
                    start_date = datetime.fromtimestamp(
                        subscription_object.trial_start, tz=pytz.UTC
                    )
                    expiry_date = datetime.fromtimestamp(
                        subscription_object.trial_end, tz=pytz.UTC
                    )
                elif getattr(subscription_object, "created", None):
                    logger.warning(
                        "Subscription %s missing period bounds, using created + recurring/trial fallback",
                        subscription_object.id,
                    )
                    start_date = datetime.fromtimestamp(
                        subscription_object.created, tz=pytz.UTC
                    )
                    inv_obj = None
                    raw_inv = (
                        getattr(session_data, "invoice", None)
                        if hasattr(session_data, "invoice")
                        else session_data.get("invoice")
                    )
                    if raw_inv:
                        inv_id = raw_inv if isinstance(raw_inv, str) else _sget(raw_inv, "id")
                        if inv_id:
                            try:
                                inv_obj = stripe.Invoice.retrieve(
                                    str(inv_id), expand=["lines.data.price"]
                                )
                            except Exception as exc:
                                logger.warning(
                                    "Could not load invoice %s for period fallback: %s",
                                    inv_id,
                                    exc,
                                )
                    expiry_date = subscription_fallback_expiry(
                        subscription_object,
                        start_date,
                        is_trial=is_trial,
                        invoice=inv_obj,
                    )
                    if expiry_date is None:
                        raise ValueError(
                            f"Subscription {subscription_object.id}: could not derive expiry "
                            f"from price/recurring or invoice lines"
                        )
                else:
                    raise ValueError(
                        f"Subscription {subscription_object.id} has no date information available"
                    )

                if is_trial:
                    logger.info(
                        f"Trial subscription detected for user {telegram_id}, trial ends at {expiry_date}"
                    )
            else:
                # One-time payment (no subscription on session): duration from Price if present, else env.
                start_date = datetime.now(pytz.UTC)
                if os.getenv('DEVELOPMENT_MODE', 'false').lower() == 'true':
                    expiry_date = start_date + timedelta(minutes=1)
                else:
                    expiry_date = None
                    line_items = getattr(session_data, "line_items", None)
                    li_data = None
                    if line_items is not None:
                        li_data = getattr(line_items, "data", None) or (
                            line_items.get("data") if hasattr(line_items, "get") else None
                        )
                    if li_data and len(li_data) > 0:
                        pr = None
                        it0 = li_data[0]
                        if hasattr(it0, "price"):
                            pr = it0.price
                        elif isinstance(it0, dict):
                            pr = it0.get("price")
                        pid = _price_id_from_obj(pr) if pr else None
                        if pid:
                            expiry_date = expiry_from_recurring_price_id(start_date, pid)
                    if expiry_date is None:
                        days = int(os.getenv("ONE_TIME_CHECKOUT_ACCESS_DAYS", "30"))
                        expiry_date = start_date + timedelta(days=days)
            
            if hasattr(session_data, 'id'):
                session_id = session_data.id
            else:
                session_id = session_data.get("id")
            
            if hasattr(session_data, 'amount_total'):
                amount_total = session_data.amount_total
            else:
                amount_total = session_data.get("amount_total", 0)
            
            if hasattr(session_data, 'currency'):
                currency = session_data.currency
            else:
                currency = session_data.get("currency", "usd")
            
            # Determine subscription type and metadata
            subscription_type = "trial" if is_trial else "premium"
            metadata_dict = {}
            if is_trial:
                metadata_dict["is_trial"] = True
                metadata_dict["trial_started_at"] = datetime.utcnow().isoformat()
            
            subscription_data = {
                "telegram_id": int(telegram_id),
                "stripe_customer_id": customer_id,
                "stripe_session_id": session_id,
                "status": "active",
                "subscription_type": subscription_type,
                "start_date": start_date,
                "expiry_date": expiry_date,
                "amount_paid": amount_total / 100,  # Convert from cents (0 for trials)
                "currency": currency,
                "updated_at": datetime.utcnow(),
                "metadata": metadata_dict if metadata_dict else None,
            }

            if subscription_object is not None:
                subscription_data["stripe_subscription_id"] = subscription_object.id
                spid = _price_id_from_stripe_subscription(subscription_object)
                if spid:
                    subscription_data["stripe_price_id"] = spid
                if customer_id:
                    removed = self.cancel_other_subscriptions_except(
                        str(customer_id), subscription_object.id
                    )
                    if removed:
                        logger.info(
                            "Post-checkout: cancelled %s other subscription(s) for customer %s",
                            removed,
                            customer_id,
                        )

            if is_trial:
                logger.info(f"Trial subscription processed for telegram user {telegram_id}, expires at {expiry_date}")
            else:
                logger.info(f"Payment processed for telegram user {telegram_id}")
            return subscription_data
            
        except Exception as e:
            logger.error(f"Error handling successful payment: {e}")
            logger.error(f"Session data type: {type(session_data)}")
            logger.error(f"Session data: {session_data}")
            return None
    
    def _sanitize_string(self, text: str) -> str:
        """Sanitize string to remove problematic Unicode characters"""
        if not text:
            return ""
        
        try:
            # Remove or replace problematic Unicode characters
            # U+2028: Line Separator, U+2029: Paragraph Separator, U+0000: Null
            problematic_chars = {
                '\u2028': ' ',  # Line Separator -> space
                '\u2029': ' ',  # Paragraph Separator -> space
                '\u0000': '',   # Null -> empty
                '\u0001': '',   # Start of Heading -> empty
                '\u0002': '',   # Start of Text -> empty
                '\u0003': '',   # End of Text -> empty
                '\u0004': '',   # End of Transmission -> empty
                '\u0005': '',   # Enquiry -> empty
                '\u0006': '',   # Acknowledge -> empty
                '\u0007': '',   # Bell -> empty
                '\u0008': '',   # Backspace -> empty
                '\u000B': '',   # Vertical Tab -> empty
                '\u000C': '',   # Form Feed -> empty
                '\u000E': '',   # Shift Out -> empty
                '\u000F': '',   # Shift In -> empty
            }
            
            sanitized = text
            for char, replacement in problematic_chars.items():
                sanitized = sanitized.replace(char, replacement)
            
            # Also remove any other control characters
            sanitized = ''.join(char for char in sanitized if ord(char) >= 32 or char in '\n\r\t')
            
            logger.info(f"Sanitized string: '{text}' -> '{sanitized}'")
            return sanitized
            
        except Exception as e:
            logger.error(f"Error sanitizing string: {e}")
            # Return a safe fallback
            return text[:50] if text else ""  # Limit length and remove any problematic chars 