#!/usr/bin/env python3
"""
Copy Stripe Customer metadata from --from-customer to --to-customer (merge into destination).

Use when a payer ended up with two Customer objects: keep the one that has the *live*
subscription, and copy bot metadata (telegram_id, etc.) from the duplicate row.

After a successful copy, use --clear-from-telegram to blank telegram_id / telegram_username
on the *source* customer so Customer.search(metadata['telegram_id']) only finds the survivor.

Then backfill Firestore from the surviving customer's subscription, e.g.:
  python3 scripts/backfill_firestore_subscription_from_stripe.py --telegram-id TELEGRAM_ID --live --force

Usage:
  python3 scripts/copy_stripe_customer_metadata.py --from-customer cus_AAA --to-customer cus_BBB
  python3 scripts/copy_stripe_customer_metadata.py --from-customer cus_AAA --to-customer cus_BBB --live
  python3 scripts/copy_stripe_customer_metadata.py ... --live --clear-from-telegram

Requires: STRIPE_SECRET_KEY (same mode as the customers: live vs test).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict

import stripe
from dotenv import load_dotenv

_REPO = Path(__file__).resolve().parent.parent


def _customer_metadata_raw(customer: Any) -> Dict[str, Any]:
    """
    Stripe Customer.metadata is a StripeObject — not a plain dict. Prefer Customer.to_dict()['metadata'].
    """
    td = getattr(customer, "to_dict", None)
    if callable(td):
        try:
            d = td()
            if isinstance(d, dict):
                meta = d.get("metadata")
                if isinstance(meta, dict):
                    return meta
        except Exception:
            pass
    meta_obj = getattr(customer, "metadata", None)
    if meta_obj is None:
        return {}
    if isinstance(meta_obj, dict):
        return meta_obj
    td2 = getattr(meta_obj, "to_dict", None)
    if callable(td2):
        try:
            d2 = td2()
            if isinstance(d2, dict):
                return d2
        except Exception:
            pass
    return {}


def _metadata_dict(customer: Any) -> Dict[str, str]:
    raw = _customer_metadata_raw(customer)
    out: Dict[str, str] = {}
    for k, v in raw.items():
        if v is not None and str(v).strip() != "":
            out[str(k)] = str(v).strip()
    return out


def main() -> int:
    load_dotenv(_REPO / ".env")
    p = argparse.ArgumentParser(description=__doc__.split("Usage:")[0].strip())
    p.add_argument("--from-customer", required=True, metavar="cus_")
    p.add_argument("--to-customer", required=True, metavar="cus_")
    p.add_argument("--live", action="store_true", help="Apply changes in Stripe.")
    p.add_argument(
        "--clear-from-telegram",
        action="store_true",
        help=(
            "After copying, set telegram_id and telegram_username to empty on --from-customer "
            "so searches only match --to-customer. Use only when you are retiring the source row."
        ),
    )
    args = p.parse_args()

    secret = os.environ.get("STRIPE_SECRET_KEY")
    if not secret:
        print("ERROR: STRIPE_SECRET_KEY not set.", file=sys.stderr)
        return 1
    stripe.api_key = secret

    fr = args.from_customer.strip()
    to = args.to_customer.strip()
    if fr == to:
        print("ERROR: --from-customer and --to-customer must differ.", file=sys.stderr)
        return 1

    try:
        src = stripe.Customer.retrieve(fr)
        dst = stripe.Customer.retrieve(to)
    except stripe.error.InvalidRequestError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    src_meta = _metadata_dict(src)
    dst_meta = _metadata_dict(dst)
    merged = {**dst_meta, **src_meta}

    print(f"From {fr}: {src_meta!r}", flush=True)
    print(f"To {to} (existing metadata): {dst_meta!r}", flush=True)
    print(f"Would set on {to}: {merged!r}", flush=True)

    if not args.live:
        print("\nDRY-RUN: pass --live to modify Stripe.", flush=True)
        return 0

    stripe.Customer.modify(to, metadata=merged)
    print(f"\nOK: updated metadata on {to}.", flush=True)

    if args.clear_from_telegram:
        stripe.Customer.modify(
            fr,
            metadata={
                "telegram_id": "",
                "telegram_username": "",
            },
        )
        print(
            f"OK: cleared telegram_id / telegram_username on source {fr} "
            f"(other metadata keys unchanged; use Dashboard to clear more if needed).",
            flush=True,
        )

    tid = merged.get("telegram_id") or dst_meta.get("telegram_id")
    if tid:
        print(
            f"\nNext: backfill Firestore for telegram_id={tid}, e.g.\n"
            f"  python3 scripts/backfill_firestore_subscription_from_stripe.py "
            f"--telegram-id {tid} --live --force",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
