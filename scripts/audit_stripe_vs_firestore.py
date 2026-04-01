#!/usr/bin/env python3
"""
Read-only audit: compare Stripe subscriptions to Firestore.

By default, lists every Stripe subscription with status active or trialing and checks
whether subscriptions/{telegram_id} exists in Firestore (and optionally users/{telegram_id}).

This does not modify any data. Use it to find paying customers who never got a Firestore
subscription document (webhook gaps, etc.).

Data model note (this repo):
  - subscriptions/{telegram_id}  — VIP / billing state written by payment webhooks
  - users/{telegram_id}          — sparse (e.g. has_used_trial); not a full census of
    everyone on Telegram. You do NOT need a users/ doc for every person.

Usage (repo root):
  python3 scripts/audit_stripe_vs_firestore.py
  python3 scripts/audit_stripe_vs_firestore.py --check-users-collection

  When Stripe customer metadata has no telegram_id, the script tries to resolve
  telegram_id from Firestore in order: subscriptions.stripe_subscription_id,
  subscriptions.stripe_customer_id, users.email (same order as payment fallbacks).

Requires: STRIPE_SECRET_KEY, GOOGLE_CLOUD_PROJECT, Firestore read access (ADC).
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

from stripe_compat import metadata_get  # noqa: E402


def _resolve_telegram_from_firestore(
    fs: Any,
    stripe_subscription_id: Optional[str],
    stripe_customer_id: Optional[str],
    email: Optional[str],
) -> Tuple[Optional[int], str]:
    """
    Try to infer telegram_id from Firestore when Stripe customer metadata.telegram_id is missing.
    Order: subscription id (most specific), customer id, users collection by email.
    """
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
        description="Audit: Stripe active/trialing subscriptions vs Firestore (read-only)."
    )
    parser.add_argument(
        "--check-users-collection",
        action="store_true",
        help="Also report when users/{telegram_id} is missing but subscriptions/ exists.",
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

    subs: List[Any] = []
    for st in ("active", "trialing"):
        subs.extend(_list_all_subscriptions(st))

    # Dedupe by subscription id (should not overlap across statuses)
    seen: Set[str] = set()
    unique: List[Any] = []
    for s in subs:
        sid = getattr(s, "id", None) or (s.get("id") if hasattr(s, "get") else None)
        if not sid or sid in seen:
            continue
        seen.add(sid)
        unique.append(s)

    missing_firestore: List[Dict[str, Any]] = []
    no_telegram_id: List[Dict[str, Any]] = []
    missing_users_doc: List[int] = []

    for sub in unique:
        sub_id = getattr(sub, "id", None) or sub.get("id")
        cust_id = getattr(sub, "customer", None) or (
            sub.get("customer") if hasattr(sub, "get") else None
        )
        if not cust_id:
            no_telegram_id.append(
                {"subscription_id": sub_id, "reason": "subscription_has_no_customer_id"}
            )
            continue

        try:
            customer = stripe.Customer.retrieve(str(cust_id))
        except Exception as e:
            no_telegram_id.append(
                {
                    "subscription_id": sub_id,
                    "customer_id": cust_id,
                    "reason": f"customer_retrieve_failed: {e}",
                }
            )
            continue

        md = getattr(customer, "metadata", None)
        telegram_id_raw = metadata_get(md, "telegram_id")
        if not telegram_id_raw:
            no_telegram_id.append(
                {
                    "subscription_id": sub_id,
                    "customer_id": cust_id,
                    "email": getattr(customer, "email", None),
                    "reason": "customer_metadata_missing_telegram_id",
                }
            )
            continue

        try:
            tid = int(telegram_id_raw)
        except (TypeError, ValueError):
            no_telegram_id.append(
                {
                    "subscription_id": sub_id,
                    "customer_id": cust_id,
                    "reason": f"invalid_telegram_id_metadata: {telegram_id_raw!r}",
                }
            )
            continue

        doc_ref = db.collection("subscriptions").document(str(tid))
        snap = doc_ref.get()
        if not snap.exists:
            missing_firestore.append(
                {
                    "telegram_id": tid,
                    "stripe_subscription_id": sub_id,
                    "stripe_customer_id": cust_id,
                    "subscription_status": getattr(sub, "status", None),
                }
            )
            continue

        if args.check_users_collection:
            uref = db.collection("users").document(str(tid))
            if not uref.get().exists:
                missing_users_doc.append(tid)

    print(f"Stripe active+trialing subscriptions (unique): {len(unique)}", flush=True)
    print("", flush=True)

    if no_telegram_id:
        print(
            f"--- Stripe customer metadata missing telegram_id ({len(no_telegram_id)}) ---",
            flush=True,
        )
        for row in no_telegram_id:
            print(f"  {row}", flush=True)
        print("", flush=True)

        try:
            from firestore_service import FirestoreService
        except ImportError as exc:
            print(f"ERROR: firestore_service: {exc}", file=sys.stderr)
            return 1
        fs = FirestoreService(project_id=project)
        print(
            "--- Firestore resolution (read-only): match Stripe sub/customer/email to telegram_id ---",
            flush=True,
        )
        resolved_n = 0
        for row in no_telegram_id:
            sub = row.get("subscription_id")
            cust = row.get("customer_id")
            em = row.get("email")
            tid, src = _resolve_telegram_from_firestore(fs, sub, cust, em)
            print(f"  sub={sub!r} customer={cust!r} email={em!r}", flush=True)
            if tid is not None:
                resolved_n += 1
                print(
                    f"    → telegram_id={tid} (via {src}). Next: set Stripe Customer metadata "
                    f"telegram_id={tid}, then:\n"
                    f"    python3 scripts/backfill_firestore_subscription_from_stripe.py "
                    f"--telegram-id {tid} --live",
                    flush=True,
                )
            else:
                print(
                    "    → no Firestore match (subscriptions by stripe_subscription_id / "
                    "stripe_customer_id, or users by email). Obtain their Telegram id manually.",
                    flush=True,
                )
            print("", flush=True)
        print(
            f"Resolved via Firestore: {resolved_n} / {len(no_telegram_id)}",
            flush=True,
        )
        print("", flush=True)

    if missing_firestore:
        print(
            f"--- MISSING Firestore subscriptions/{{telegram_id}} ({len(missing_firestore)}) ---",
            flush=True,
        )
        for row in missing_firestore:
            print(f"  {row}", flush=True)
        print("", flush=True)
        print(
            "Backfill one user: python3 scripts/backfill_firestore_subscription_from_stripe.py "
            "--telegram-id <id> --live",
            flush=True,
        )
    elif no_telegram_id:
        print(
            "SUMMARY: Stripe metadata.telegram_id is still missing for the customers above. "
            "If Firestore resolution found a telegram_id, add that key to the Stripe Customer "
            "and run the backfill command. If resolution failed, set metadata manually after "
            "you get their id (e.g. support / @userinfobot).",
            flush=True,
        )
    else:
        print(
            "OK: Every Stripe active/trialing subscription has customer metadata.telegram_id "
            "and a matching Firestore subscriptions/ doc.",
            flush=True,
        )

    if args.check_users_collection:
        if missing_users_doc:
            print(
                f"",
                flush=True,
            )
            print(
                f"INFO: users/ missing for these telegram_ids (subscriptions/ exists): {sorted(set(missing_users_doc))}",
                flush=True,
            )
            print(
                "(Normal if they never triggered a write to users/ — not required for VIP access.)",
                flush=True,
            )
        else:
            print("", flush=True)
            print("OK: users/ doc exists for every audited telegram_id (or --check-users-collection had nothing to compare).", flush=True)

    return 0 if not missing_firestore and not no_telegram_id else 1


if __name__ == "__main__":
    raise SystemExit(main())
