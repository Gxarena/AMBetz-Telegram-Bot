#!/usr/bin/env python3
"""
Reconcile Firestore subscriptions/{telegram_id} with Stripe's current active/trialing subscription
for the same stripe_customer_id.

Default targets: Executive (972720463) and Drose (1679019590). Override with --telegram-ids.

Requires: STRIPE_SECRET_KEY, GOOGLE_CLOUD_PROJECT, Firestore + Stripe API access.

Dry-run by default; pass --live to write Firestore updates.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

import stripe
from dotenv import load_dotenv


def _period_bounds_unix(sub: Any) -> Tuple[Optional[int], Optional[int]]:
    try:
        cs = getattr(sub, "current_period_start", None)
        ce = getattr(sub, "current_period_end", None)
        if cs is not None and ce is not None:
            return int(cs), int(ce)
    except (TypeError, ValueError):
        pass
    items = getattr(sub, "items", None)
    data = getattr(items, "data", None) if items is not None else None
    if not data:
        return None, None
    it0 = data[0]
    try:
        cs = getattr(it0, "current_period_start", None)
        ce = getattr(it0, "current_period_end", None)
        if cs is not None and ce is not None:
            return int(cs), int(ce)
    except (TypeError, ValueError, IndexError):
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


def _pick_subscription_id(customer_id: str) -> Tuple[Optional[str], str]:
    """
    Returns (subscription_id, note). If multiple active/trialing, picks the one with latest period_end.
    """
    candidates: List[str] = []
    for st in ("active", "trialing"):
        for sub in _list_subscriptions(customer_id, st):
            candidates.append(sub.id)
    if not candidates:
        return None, "no_active_or_trialing"
    if len(candidates) == 1:
        return candidates[0], "single_match"

    best_id: Optional[str] = None
    best_end = 0
    for sid in candidates:
        full = stripe.Subscription.retrieve(sid, expand=["items.data"])
        _, pe = _period_bounds_unix(full)
        if pe is not None and pe >= best_end:
            best_end = pe
            best_id = sid
    if best_id:
        return best_id, f"picked_latest_period_end_among_{len(candidates)}"
    return candidates[0], f"fallback_first_of_{len(candidates)}"


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Sync Firestore subscription docs from Stripe active/trialing subscription."
    )
    parser.add_argument(
        "--telegram-ids",
        type=str,
        default="972720463,1679019590",
        help="Comma-separated Telegram user ids (default: Executive and Drose).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Write Firestore updates (default is dry-run).",
    )
    args = parser.parse_args()

    secret = os.environ.get("STRIPE_SECRET_KEY")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not secret:
        print("ERROR: STRIPE_SECRET_KEY not set.", file=sys.stderr)
        return 1
    if not project:
        print("ERROR: GOOGLE_CLOUD_PROJECT not set.", file=sys.stderr)
        return 1

    stripe.api_key = secret

    try:
        from google.cloud import firestore
    except ImportError as exc:
        print(f"ERROR: google-cloud-firestore: {exc}", file=sys.stderr)
        return 1

    db = firestore.Client(project=project)
    telegram_ids = [int(x.strip()) for x in args.telegram_ids.split(",") if x.strip()]

    dry_run = not args.live
    print(f"Mode: {'DRY-RUN' if dry_run else 'LIVE'}", flush=True)

    for tid in telegram_ids:
        print("", flush=True)
        print(f"--- telegram_id={tid} ---", flush=True)
        doc_ref = db.collection("subscriptions").document(str(tid))
        snap = doc_ref.get()
        if not snap.exists:
            print(f"ERROR: No Firestore document subscriptions/{tid}", file=sys.stderr)
            return 1
        data = snap.to_dict() or {}
        cust = data.get("stripe_customer_id")
        if not cust:
            print("ERROR: Missing stripe_customer_id in Firestore", file=sys.stderr)
            return 1

        sub_id, pick_note = _pick_subscription_id(str(cust))
        if not sub_id:
            print(f"ERROR: No active/trialing subscription for customer {cust}", file=sys.stderr)
            return 1

        full = stripe.Subscription.retrieve(sub_id, expand=["items.data"])
        cps, cpe = _period_bounds_unix(full)
        if cps is None or cpe is None:
            print(f"ERROR: Could not read period bounds for {sub_id}", file=sys.stderr)
            return 1

        start_dt = datetime.fromtimestamp(cps, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(cpe, tz=timezone.utc)

        print(f"Stripe customer: {cust}", flush=True)
        print(f"Canonical subscription: {sub_id} ({pick_note})", flush=True)
        print(f"Firestore stripe_subscription_id was: {data.get('stripe_subscription_id')!r}", flush=True)
        print(f"Will set start_date={start_dt.isoformat()} expiry_date={end_dt.isoformat()} status=active", flush=True)

        if dry_run:
            continue

        doc_ref.update(
            {
                "stripe_subscription_id": sub_id,
                "status": "active",
                "start_date": start_dt,
                "expiry_date": end_dt,
                "updated_at": datetime.now(timezone.utc),
                "metadata": {},
            }
        )
        print("Firestore updated.", flush=True)

    print("", flush=True)
    print("Done.", flush=True)
    if dry_run:
        print("Re-run with --live to apply.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
