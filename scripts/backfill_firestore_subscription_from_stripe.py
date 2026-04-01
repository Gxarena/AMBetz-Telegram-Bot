#!/usr/bin/env python3
"""
Create or replace Firestore subscriptions/{telegram_id} from Stripe when the webhook
never wrote the document (Stripe has active/trialing sub, Firestore missing or wrong).

Looks up Stripe Customer via metadata['telegram_id'], picks the canonical active/trialing
Subscription (latest current_period_end if several), then calls FirestoreService.upsert_subscription.

Usage (repo root):
  python3 scripts/backfill_firestore_subscription_from_stripe.py --telegram-id 6961106092
  python3 scripts/backfill_firestore_subscription_from_stripe.py --telegram-id 6961106092 --live

Requires:
  STRIPE_SECRET_KEY (must match Stripe mode: sk_live_* for production customers)
  GOOGLE_CLOUD_PROJECT
  Application Default Credentials with Firestore access (gcloud auth application-default login)

Default: dry-run. Pass --live to write Firestore.
If subscriptions/{telegram_id} already exists with status=active, use --force to overwrite.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

import stripe
from dotenv import load_dotenv

# Repo-root imports
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))


def _stripe_field(obj: Any, key: str) -> Any:
    """Read Stripe field across dict-like / object SDK shapes."""
    if obj is None:
        return None
    if hasattr(obj, "get"):
        v = obj.get(key)
        if v is not None:
            return v
    v = getattr(obj, key, None)
    if v is not None:
        return v
    if hasattr(obj, "__getitem__"):
        try:
            return obj[key]
        except (KeyError, TypeError):
            pass
    return None


def _subscription_items_object(sub: Any) -> Any:
    """
    Subscription `items` list object. On StripeObject dict subclasses, `sub.items` is dict.items —
    use sub['items'] or a non-callable attribute.
    """
    try:
        return sub["items"]
    except (KeyError, TypeError):
        pass
    cand = getattr(sub, "items", None)
    if cand is not None and not callable(cand):
        return cand
    return None


def _subscription_items_data(sub: Any) -> List[Any]:
    items_obj = _subscription_items_object(sub)
    if items_obj is None:
        return []
    data = _stripe_field(items_obj, "data")
    if not data:
        return []
    return list(data)


def _period_bounds_unix(sub: Any) -> Tuple[Optional[int], Optional[int]]:
    """Top-level subscription period, then each line item (matches gcp_stripe_service intent)."""
    try:
        cs = _stripe_field(sub, "current_period_start")
        ce = _stripe_field(sub, "current_period_end")
        if cs is not None and ce is not None:
            return int(cs), int(ce)
    except (TypeError, ValueError):
        pass
    for it0 in _subscription_items_data(sub):
        try:
            cs = _stripe_field(it0, "current_period_start")
            ce = _stripe_field(it0, "current_period_end")
            if cs is not None and ce is not None:
                return int(cs), int(ce)
        except (TypeError, ValueError, IndexError):
            continue
    return None, None


def _period_from_latest_invoice(sub: Any) -> Tuple[Optional[int], Optional[int]]:
    """When Subscription omits periods, use latest invoice period_start/end."""
    li = getattr(sub, "latest_invoice", None)
    inv_id = li if isinstance(li, str) else (getattr(li, "id", None) if li else None)
    if not inv_id:
        return None, None
    try:
        inv = stripe.Invoice.retrieve(inv_id)
        ps = getattr(inv, "period_start", None)
        pe = getattr(inv, "period_end", None)
        if ps is not None and pe is not None and ps != pe:
            return int(ps), int(pe)
    except Exception:
        pass
    return None, None


def _list_subscriptions(customer_id: str, status: str) -> List[Any]:
    out: List[Any] = []
    params: dict = {"customer": customer_id, "status": status, "limit": 100}
    while True:
        page = stripe.Subscription.list(**params)
        batch = page.data or []
        out.extend(batch)
        if not getattr(page, "has_more", False) or not batch:
            break
        params["starting_after"] = batch[-1].id
    return out


def _pick_subscription(customer_id: str) -> Tuple[Optional[Any], str]:
    """Return (subscription object or None, note)."""
    candidates: List[Any] = []
    for st in ("active", "trialing"):
        candidates.extend(_list_subscriptions(customer_id, st))
    if not candidates:
        return None, "no_active_or_trialing"
    _exp = ["items.data", "items.data.price", "latest_invoice"]
    if len(candidates) == 1:
        full = stripe.Subscription.retrieve(candidates[0].id, expand=_exp)
        return full, "single_match"

    best: Optional[Any] = None
    best_end = 0
    for c in candidates:
        full = stripe.Subscription.retrieve(c.id, expand=_exp)
        _, pe = _period_bounds_unix(full)
        if pe is not None and pe >= best_end:
            best_end = pe
            best = full
    if best:
        best = stripe.Subscription.retrieve(best.id, expand=_exp)
        return best, f"picked_latest_period_end_among_{len(candidates)}"
    full = stripe.Subscription.retrieve(candidates[0].id, expand=_exp)
    return full, f"fallback_first_of_{len(candidates)}"


def _trial_bounds(sub: Any) -> Tuple[Optional[int], Optional[int]]:
    ts = _stripe_field(sub, "trial_start")
    te = _stripe_field(sub, "trial_end")
    if ts is not None and te is not None:
        return int(ts), int(te)
    return None, None


def _amount_currency(sub: Any) -> Tuple[Optional[float], Optional[str]]:
    li = getattr(sub, "latest_invoice", None)
    if not li:
        return None, None
    inv_id = li if isinstance(li, str) else getattr(li, "id", None)
    if not inv_id:
        return None, None
    try:
        inv = stripe.Invoice.retrieve(inv_id)
        ap = getattr(inv, "amount_paid", None)
        cur = getattr(inv, "currency", None)
        if ap is not None:
            return float(ap) / 100.0, (cur or "").lower() or None
    except Exception:
        pass
    return None, None


def main() -> int:
    load_dotenv(_REPO / ".env")
    parser = argparse.ArgumentParser(
        description="Backfill Firestore subscription from Stripe customer metadata[telegram_id]."
    )
    parser.add_argument(
        "--telegram-id",
        type=int,
        required=True,
        help="Telegram user id (must match Stripe customer metadata telegram_id).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Write Firestore (default is dry-run).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing Firestore doc if status is already active.",
    )
    args = parser.parse_args()

    tid = args.telegram_id
    secret = os.environ.get("STRIPE_SECRET_KEY")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not secret:
        print("ERROR: STRIPE_SECRET_KEY not set.", file=sys.stderr)
        return 1
    if not project:
        print("ERROR: GOOGLE_CLOUD_PROJECT not set.", file=sys.stderr)
        return 1

    stripe.api_key = secret

    from firestore_service import FirestoreService
    from gcp_stripe_service import subscription_fallback_expiry

    fs = FirestoreService(project)
    existing = fs.get_subscription(tid)
    if existing and existing.get("status") == "active" and not args.force:
        print(
            f"Firestore already has active subscription for {tid}. "
            f"Re-run with --force to replace from Stripe.",
            file=sys.stderr,
        )
        print(f"Current: expiry_date={existing.get('expiry_date')!r}", file=sys.stderr)
        return 1

    print(f"Looking up Stripe customer with metadata telegram_id={tid} ...", flush=True)
    res = stripe.Customer.search(query=f"metadata['telegram_id']:'{tid}'", limit=5)
    if not res.data:
        print(
            "ERROR: No Stripe customer with metadata['telegram_id'] matching this user. "
            "Check Stripe Dashboard → Customers → Metadata.",
            file=sys.stderr,
        )
        return 1
    if len(res.data) > 1:
        print(
            f"WARNING: Multiple Stripe customers share telegram_id {tid}; using first: {res.data[0].id}",
            file=sys.stderr,
        )
    customer = res.data[0]
    cust_id = customer.id

    sub, pick_note = _pick_subscription(cust_id)
    if not sub:
        print(f"ERROR: No active or trialing subscription for customer {cust_id}.", file=sys.stderr)
        return 1

    # Fresh retrieve so line items/periods are always expanded (avoids list() stub objects).
    sub_id = _stripe_field(sub, "id")
    if not sub_id:
        print("ERROR: Subscription has no id.", file=sys.stderr)
        return 1
    sub = stripe.Subscription.retrieve(
        sub_id, expand=["items.data", "items.data.price", "latest_invoice"]
    )

    status = (getattr(sub, "status", None) or _stripe_field(sub, "status") or "") or ""
    cps, cpe = _period_bounds_unix(sub)
    if cps is None or cpe is None:
        cps, cpe = _period_from_latest_invoice(sub)
    trial_s, trial_e = _trial_bounds(sub)

    period_fallback = False
    if status == "trialing" and trial_s is not None and trial_e is not None:
        start_dt = datetime.fromtimestamp(trial_s, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(trial_e, tz=timezone.utc)
        sub_type = "trial"
    elif cps is not None and cpe is not None:
        start_dt = datetime.fromtimestamp(cps, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(cpe, tz=timezone.utc)
        sub_type = "premium"
    else:
        # Same heuristic as gcp_stripe_service.subscription_fallback_expiry (recurring-aware)
        created = _stripe_field(sub, "created")
        if created is not None and status in ("active", "trialing"):
            start_dt = datetime.fromtimestamp(int(created), tz=timezone.utc)
            is_tr = status == "trialing"
            end_dt = subscription_fallback_expiry(sub, start_dt, is_trial=is_tr)
            sub_type = "trial" if is_tr else "premium"
            period_fallback = True
            print(
                "WARNING: Using subscription.created + recurring/trial fallback for period "
                "(API did not return item periods). Verify expiry in Stripe Dashboard.",
                file=sys.stderr,
            )
        else:
            print(
                f"ERROR: Could not derive period or trial bounds for {sub_id}. "
                f"Check subscription in Stripe Dashboard (API shape).",
                file=sys.stderr,
            )
            return 1

    amount, currency = _amount_currency(sub)

    # Reference id: prefer latest invoice id (matches recurring webhook style); else subscription id
    session_ref: Optional[str] = None
    li = getattr(sub, "latest_invoice", None)
    if li:
        session_ref = li if isinstance(li, str) else getattr(li, "id", None)

    print("", flush=True)
    print(f"  Stripe customer:     {cust_id}", flush=True)
    print(f"  Subscription:        {sub_id} ({pick_note})", flush=True)
    print(f"  Status:              {status}", flush=True)
    print(f"  subscription_type:   {sub_type}", flush=True)
    print(f"  start_date:          {start_dt.isoformat()}", flush=True)
    print(f"  expiry_date:         {end_dt.isoformat()}", flush=True)
    print(f"  amount_paid/currency:{amount!r} {currency!r}", flush=True)
    print(f"  stripe_session_id:   {session_ref!r} (invoice or ref)", flush=True)

    if not args.live:
        print("", flush=True)
        print("DRY-RUN: no Firestore write. Pass --live to apply.", flush=True)
        return 0

    meta = {"backfilled_from_stripe": datetime.now(timezone.utc).isoformat()}
    if period_fallback:
        meta["period_fallback_created_plus_days"] = True

    ok = fs.upsert_subscription(
        telegram_id=tid,
        start_date=start_dt,
        expiry_date=end_dt,
        subscription_type=sub_type,
        metadata=meta,
        stripe_customer_id=cust_id,
        stripe_session_id=session_ref,
        stripe_subscription_id=sub_id,
        amount_paid=amount,
        currency=currency,
    )
    if not ok:
        print("ERROR: upsert_subscription returned False.", file=sys.stderr)
        return 1
    print("", flush=True)
    print(f"OK: Firestore subscriptions/{tid} written.", flush=True)
    print(
        "Note: This does not send Telegram VIP invite links. User can use /status; "
        "you may need to send links manually or trigger your bot flow.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
