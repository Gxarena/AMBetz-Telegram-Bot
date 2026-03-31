#!/usr/bin/env python3
"""
One-off cleanup: cancel known orphan Stripe subscriptions for Executive and Drose
while keeping the subscription id stored in Firestore.

Safety:
  - Default is dry-run (--live required to cancel).
  - Unless --skip-firestore-check, loads Firestore subscriptions/{telegram_id} and
    verifies stripe_customer_id + stripe_subscription_id match expectations before canceling.

Env:
  STRIPE_SECRET_KEY (required)
  GOOGLE_CLOUD_PROJECT (required unless --skip-firestore-check)

Run from repo root: python3 scripts/fix_executive_drose_orphan_subscriptions.py
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Optional

import stripe
from dotenv import load_dotenv

# Orphan subscription ids to cancel (from diagnose). Tracked sub + customer come from Firestore.
FIXUPS: List[Dict[str, Any]] = [
    {
        "label": "Executive",
        "telegram_id": 972720463,
        "cancel_subscription_id": "sub_1T36fbF7amkBfz0LnOG23Wfr",
    },
    {
        "label": "Drose",
        "telegram_id": 1679019590,
        "cancel_subscription_id": "sub_1SCj8GF7amkBfz0LJnvtYw7M",
    },
]


def _get_firestore_subscription(db: Any, telegram_id: int) -> Optional[Dict[str, Any]]:
    doc = db.collection("subscriptions").document(str(telegram_id)).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    data["_doc_id"] = doc.id
    return data


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Cancel orphan Stripe subs for Executive & Drose (dry-run by default)."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually call Stripe Subscription.cancel. Without this, dry-run only.",
    )
    parser.add_argument(
        "--skip-firestore-check",
        action="store_true",
        help="Do not load Firestore (unsafe). Only use if you cannot access GCP from this machine.",
    )
    args = parser.parse_args()

    if args.live and args.skip_firestore_check:
        print(
            "ERROR: Refusing --live with --skip-firestore-check (unsafe). Use Firestore validation.",
            file=sys.stderr,
        )
        return 1

    secret = os.environ.get("STRIPE_SECRET_KEY")
    if not secret:
        print("ERROR: STRIPE_SECRET_KEY is not set.", file=sys.stderr)
        return 1

    stripe.api_key = secret

    db = None
    if not args.skip_firestore_check:
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not project:
            print("ERROR: GOOGLE_CLOUD_PROJECT is not set (or pass --skip-firestore-check).", file=sys.stderr)
            return 1
        try:
            from google.cloud import firestore

            db = firestore.Client(project=project)
        except Exception as exc:
            print(f"ERROR: Firestore init failed: {exc}", file=sys.stderr)
            return 1

    dry_run = not args.live
    print(f"Mode: {'DRY-RUN' if dry_run else 'LIVE (will cancel subscriptions)'}", flush=True)

    for row in FIXUPS:
        label = row["label"]
        tid = row["telegram_id"]
        cancel_id = row["cancel_subscription_id"]

        print("", flush=True)
        print(f"--- {label} (telegram_id={tid}) ---", flush=True)

        exp_sub = ""
        exp_cust = ""
        if db is not None:
            fs = _get_firestore_subscription(db, tid)
            if not fs:
                print(f"ERROR: No Firestore document subscriptions/{tid}", file=sys.stderr)
                return 1
            exp_cust = fs.get("stripe_customer_id") or ""
            exp_sub = fs.get("stripe_subscription_id") or ""
            if not exp_cust or not exp_sub:
                print(
                    "ERROR: Firestore doc missing stripe_customer_id or stripe_subscription_id",
                    file=sys.stderr,
                )
                return 1
            print(f"Firestore: customer={exp_cust} tracked_sub={exp_sub}", flush=True)
        else:
            print("WARN: Skipping Firestore validation (--skip-firestore-check)", flush=True)

        try:
            orphan = stripe.Subscription.retrieve(cancel_id)
        except stripe.StripeError as exc:
            print(f"ERROR: Cannot retrieve {cancel_id}: {exc}", file=sys.stderr)
            return 1

        ocust = orphan.customer
        if isinstance(ocust, stripe.Customer):
            ocust = ocust.id

        if db is not None:
            if ocust != exp_cust:
                print(
                    f"ERROR: Orphan sub {cancel_id} customer={ocust!r} != Firestore customer={exp_cust!r}",
                    file=sys.stderr,
                )
                return 1
            if cancel_id == exp_sub:
                print(
                    "ERROR: Refusing to cancel: orphan id equals Firestore stripe_subscription_id.",
                    file=sys.stderr,
                )
                return 1

        action = "would cancel" if dry_run else "will CANCEL"
        print(
            f"Orphan sub {cancel_id} status={orphan.status} customer={ocust} — "
            f"{action} (tracked sub {exp_sub or '(unknown)'} left as-is).",
            flush=True,
        )

        if dry_run:
            continue

        try:
            stripe.Subscription.cancel(cancel_id)
            print(f"Cancelled {cancel_id}", flush=True)
        except stripe.StripeError as exc:
            print(f"ERROR: Cancel failed: {exc}", file=sys.stderr)
            return 1

    print("", flush=True)
    print("Done.", flush=True)
    if dry_run:
        print("Re-run with --live after confirming output above.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
