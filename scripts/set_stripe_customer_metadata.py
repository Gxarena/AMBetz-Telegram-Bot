#!/usr/bin/env python3
"""
Set or merge bot-related metadata on a Stripe Customer (dry-run unless --live).

Typical use after fixing email ↔ Telegram in Firestore, before backfill:

  python3 scripts/set_stripe_customer_metadata.py \\
    --customer cus_XXX --telegram-id 7719843319 --telegram-username someuser
  python3 scripts/set_stripe_customer_metadata.py ... --live

Merges with existing Customer metadata; new keys overwrite same-named keys.

Requires: STRIPE_SECRET_KEY in .env (live vs test must match the Customer).

Usage:
  python3 scripts/set_stripe_customer_metadata.py --customer cus_XXX --telegram-id 123 --live
  python3 scripts/set_stripe_customer_metadata.py --customer cus_XXX --telegram-id 123 --meta note=manual
  python3 scripts/set_stripe_customer_metadata.py --customer cus_XXX --telegram-id 5880689445 --telegram-username abath3r --name \"AB\" --live
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
    td = getattr(customer, "to_dict", None)
    if callable(td):
        try:
            d = td()
            if isinstance(d, dict):
                meta = d.get("metadata")
                if isinstance(meta, dict):
                    return dict(meta)
        except Exception:
            pass
    meta_obj = getattr(customer, "metadata", None)
    if meta_obj is None:
        return {}
    if isinstance(meta_obj, dict):
        return dict(meta_obj)
    td2 = getattr(meta_obj, "to_dict", None)
    if callable(td2):
        try:
            d2 = td2()
            if isinstance(d2, dict):
                return dict(d2)
        except Exception:
            pass
    return {}


def _clean_str_dict(d: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in d.items():
        if v is None:
            continue
        s = str(v).strip()
        if s:
            out[str(k)] = s
    return out


def main() -> int:
    load_dotenv(_REPO / ".env")
    p = argparse.ArgumentParser(
        description="Merge telegram/source metadata onto a Stripe Customer (see docstring)."
    )
    p.add_argument("--customer", required=True, metavar="cus_", help="Stripe Customer id.")
    p.add_argument("--telegram-id", type=int, required=True, help="Telegram chat/user id.")
    p.add_argument("--telegram-username", default="", help="Username without @ (optional).")
    p.add_argument(
        "--source",
        default="gcp-bot",
        help="metadata source value (default: gcp-bot).",
    )
    p.add_argument(
        "--meta",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Extra metadata pair (repeatable).",
    )
    p.add_argument(
        "--name",
        default="",
        help="Set Stripe Customer name (display; e.g. 'AB' for first-name-only). Merged on live modify.",
    )
    p.add_argument("--live", action="store_true", help="Call Stripe Customer.modify.")
    args = p.parse_args()

    secret = os.environ.get("STRIPE_SECRET_KEY")
    if not secret:
        print("ERROR: STRIPE_SECRET_KEY not set.", file=sys.stderr)
        return 1
    stripe.api_key = secret

    cus = args.customer.strip()
    try:
        obj = stripe.Customer.retrieve(cus)
    except stripe.error.InvalidRequestError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print("Hint: copy cus_ from the Stripe URL; ids are case-sensitive (I vs l, etc.).", file=sys.stderr)
        return 1

    before = _clean_str_dict(_customer_metadata_raw(obj))
    updates: Dict[str, str] = {
        "telegram_id": str(args.telegram_id),
        "source": str(args.source),
    }
    if args.telegram_username.strip():
        updates["telegram_username"] = args.telegram_username.strip()

    for pair in args.meta:
        if "=" not in pair:
            print(f"ERROR: --meta expects KEY=VALUE, got {pair!r}", file=sys.stderr)
            return 1
        k, _, v = pair.partition("=")
        k, v = k.strip(), v.strip()
        if k:
            updates[k] = v

    merged = {**before, **updates}

    print(f"Customer {cus}", flush=True)
    print(f"  metadata before: {before}", flush=True)
    print(f"  metadata after:  {merged}", flush=True)

    if not args.live:
        print("\nDRY-RUN: pass --live to apply.", flush=True)
        return 0

    kwargs: Dict[str, Any] = {"metadata": merged}
    if args.name.strip():
        kwargs["name"] = args.name.strip()
        print(f"  setting name: {args.name.strip()!r}", flush=True)

    stripe.Customer.modify(cus, **kwargs)
    print("\nOK: Stripe Customer updated.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
