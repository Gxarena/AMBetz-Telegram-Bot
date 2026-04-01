#!/usr/bin/env python3
"""
For Stripe Customers who pay but lack metadata.telegram_id: resolve telegram_id from Firestore
(subscription id, then customer id on subscriptions/, then users/ email) — same order as
audit_stripe_vs_firestore.py.

Dry-run by default. With --live, sets Stripe Customer metadata telegram_id (merged with
existing Stripe metadata). Then you must backfill Firestore:

  python3 scripts/backfill_firestore_subscription_from_stripe.py --telegram-id <id> --live

Usage:
  python3 scripts/repair_stripe_customer_telegram_from_firestore.py
  python3 scripts/repair_stripe_customer_telegram_from_firestore.py --live

Requires: STRIPE_SECRET_KEY, GOOGLE_CLOUD_PROJECT, ADC with Firestore + Stripe access.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import stripe
from dotenv import load_dotenv

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from firestore_service import FirestoreService  # noqa: E402
from stripe_compat import metadata_get  # noqa: E402


def _resolve_telegram_from_firestore(
    fs: FirestoreService,
    stripe_subscription_id: Optional[str],
    stripe_customer_id: Optional[str],
    email: Optional[str],
) -> Tuple[Optional[int], str]:
    if stripe_subscription_id:
        doc = fs.get_subscription_by_stripe_subscription_id(str(stripe_subscription_id))
        if doc and doc.get("telegram_id") is not None:
            return int(doc["telegram_id"]), "subscriptions[stripe_subscription_id]"
    if stripe_customer_id:
        doc = fs.get_subscription_by_stripe_customer(str(stripe_customer_id))
        if doc and doc.get("telegram_id") is not None:
            return int(doc["telegram_id"]), "subscriptions[stripe_customer_id]"
    if email and str(email).strip():
        e = str(email).strip()
        for candidate in (e, e.lower()):
            user = fs.get_user_by_email(candidate)
            if user and user.get("telegram_id") is not None:
                return int(user["telegram_id"]), "users[email]"
    return None, "not_found"


def _list_all_subscriptions(status: str) -> List[Any]:
    out: List[Any] = []
    params: dict = {"status": status, "limit": 100}
    while True:
        page = stripe.Subscription.list(**params)
        batch = page.data or []
        out.extend(batch)
        if not getattr(page, "has_more", False) or not batch:
            break
        params["starting_after"] = batch[-1].id
    return out


def main() -> int:
    load_dotenv(_REPO / ".env")
    parser = argparse.ArgumentParser(
        description="Set Stripe customer metadata.telegram_id from Firestore (sub id, customer id, or email)."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Apply Stripe Customer metadata updates (default: dry-run).",
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
    fs = FirestoreService(project_id=project)

    subs: List[Any] = []
    for st in ("active", "trialing"):
        subs.extend(_list_all_subscriptions(st))

    seen: Set[str] = set()
    unique: List[Any] = []
    for s in subs:
        sid = getattr(s, "id", None) or (s.get("id") if hasattr(s, "get") else None)
        if not sid or sid in seen:
            continue
        seen.add(sid)
        unique.append(s)

    to_fix: List[Dict[str, Any]] = []
    for sub in unique:
        sub_id = getattr(sub, "id", None) or sub.get("id")
        cust_id = getattr(sub, "customer", None) or (
            sub.get("customer") if hasattr(sub, "get") else None
        )
        if not cust_id:
            continue
        try:
            customer = stripe.Customer.retrieve(str(cust_id))
        except Exception as e:
            print(f"SKIP retrieve failed {cust_id}: {e}", flush=True)
            continue
        if metadata_get(getattr(customer, "metadata", None), "telegram_id"):
            continue
        email = getattr(customer, "email", None)
        tid, src = _resolve_telegram_from_firestore(fs, sub_id, str(cust_id), email)
        to_fix.append(
            {
                "subscription_id": sub_id,
                "customer_id": str(cust_id),
                "email": email,
                "suggested_telegram_id": tid,
                "resolve_source": src,
            }
        )

    mode = "LIVE" if args.live else "DRY-RUN"
    print(f"Mode: {mode}", flush=True)
    print(f"Unlinked active/trialing subscriptions (no telegram_id on customer): {len(to_fix)}", flush=True)
    print("", flush=True)

    applied = 0
    no_match = 0
    for row in to_fix:
        cid = row["customer_id"]
        tid = row["suggested_telegram_id"]
        print(f"--- {cid} sub={row['subscription_id']} email={row['email']!r} ---", flush=True)
        if tid is None:
            print(
                "  No Firestore match (stripe_subscription_id, stripe_customer_id on subscriptions/, "
                "or users/ by email). Obtain Telegram id manually, set Customer metadata, then backfill.",
                flush=True,
            )
            no_match += 1
            continue

        print(
            f"  Firestore suggests telegram_id={tid} (via {row.get('resolve_source', '?')})",
            flush=True,
        )
        if args.live:
            try:
                stripe.Customer.modify(cid, metadata={"telegram_id": str(tid)})
                print(f"  OK: Stripe customer metadata updated.", flush=True)
                applied += 1
            except Exception as e:
                print(f"  ERROR: {e}", file=sys.stderr, flush=True)
        else:
            print(
                f"  Would run: stripe.Customer.modify({cid!r}, metadata={{'telegram_id': {tid!r}}})",
                flush=True,
            )
        print(
            f"  Optional sync from Stripe → Firestore (if doc missing or you want canonical dates):\n"
            f"    python3 scripts/backfill_firestore_subscription_from_stripe.py --telegram-id {tid} --live\n"
            f"  If backfill says an active doc already exists, you can skip it (Stripe metadata fix was the important part).\n"
            f"  To overwrite Firestore from Stripe anyway: add --force to that command.",
            flush=True,
        )
        print("", flush=True)

    if not args.live:
        print("Re-run with --live to apply Stripe metadata for rows that had a Firestore match.", flush=True)
    else:
        print(f"Stripe metadata updates applied: {applied}", flush=True)
        if applied:
            print(
                "Stripe will now include telegram_id on webhooks for those customers. "
                "Backfill is only needed if subscriptions/ was missing or out of date.",
                flush=True,
            )
    if no_match:
        print(f"Customers still needing manual telegram_id: {no_match}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
