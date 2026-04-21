#!/usr/bin/env python3
"""
Read-only audit: Stripe active/trialing subscriptions billed at CAD $35 or $50 vs Firestore.

Matches the VIP weekly ($35) and biweekly ($50) tiers by *first* subscription line item:
unit_amount in cents and currency "cad". Compares Stripe period end (including flexible
billing / per-item periods) to Firestore subscriptions/{telegram_id}.expiry_date and
checks stripe_subscription_id.

Usage (repo root):
  python3 scripts/audit_cad_35_50_firestore.py
  python3 scripts/audit_cad_35_50_firestore.py --tolerance-seconds 3600

Requires: STRIPE_SECRET_KEY, GOOGLE_CLOUD_PROJECT, ADC with Firestore read access.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import stripe
from dotenv import load_dotenv

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from gcp_stripe_service import _subscription_period_bounds_unix  # noqa: E402
from stripe_compat import metadata_get  # noqa: E402


def _list_subscriptions_paginated(status: str) -> List[Any]:
    out: List[Any] = []
    params: Dict[str, Any] = {"status": status, "limit": 100}
    while True:
        page = stripe.Subscription.list(**params)
        batch = page.data or []
        out.extend(batch)
        if not getattr(page, "has_more", False) or not batch:
            break
        params["starting_after"] = batch[-1].id
    return out


def _first_line_price_amount_currency(sub: Any) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """(unit_amount_cents, currency, price_id) from first subscription item."""
    items = sub.get("items") if hasattr(sub, "get") else None
    if items is None:
        items = getattr(sub, "items", None)
    data = None
    if items is not None:
        data = items.get("data") if hasattr(items, "get") else getattr(items, "data", None)
    if not data:
        return None, None, None
    it0 = data[0]
    price = it0.get("price") if hasattr(it0, "get") else getattr(it0, "price", None)
    if price is None:
        return None, None, None
    if isinstance(price, str):
        try:
            pr = stripe.Price.retrieve(price)
        except Exception:
            return None, None, price
        ua = getattr(pr, "unit_amount", None)
        cur = getattr(pr, "currency", None)
        return int(ua) if ua is not None else None, (cur or "").lower() or None, pr.id
    ua = price.get("unit_amount") if hasattr(price, "get") else getattr(price, "unit_amount", None)
    cur = price.get("currency") if hasattr(price, "get") else getattr(price, "currency", None)
    pid = price.get("id") if hasattr(price, "get") else getattr(price, "id", None)
    if ua is None:
        return None, (str(cur).lower() if cur else None), str(pid) if pid else None
    return int(ua), (str(cur).lower() if cur else None), str(pid) if pid else None


def _dt_to_utc_timestamp(dt: Any) -> Optional[int]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        return int(dt.timestamp())
    return None


def main() -> int:
    load_dotenv(_REPO / ".env")
    parser = argparse.ArgumentParser(
        description="Audit CAD $35/$50 Stripe subs vs Firestore expiry + stripe_subscription_id."
    )
    parser.add_argument(
        "--tolerance-seconds",
        type=int,
        default=120,
        help="Max |stripe_period_end - firestore_expiry| to still count OK (default 120).",
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

    from firestore_service import FirestoreService

    fs = FirestoreService(project_id=project)

    target_amounts = {3500, 5000}  # CAD cents: $35, $50
    target_currency = "cad"

    stubs: List[Any] = []
    for st in ("active", "trialing"):
        stubs.extend(_list_subscriptions_paginated(st))

    seen: set = set()
    rows_checked = 0
    issues: List[str] = []
    ok_n = 0

    print(
        f"Scanning {len(stubs)} Stripe subscription rows (active+trialing, may include duplicates)…",
        flush=True,
    )
    print(
        f"Filter: first line item {sorted(target_amounts)} cents, currency={target_currency!r}",
        flush=True,
    )
    print("", flush=True)

    for stub in stubs:
        sid = getattr(stub, "id", None) or (stub.get("id") if hasattr(stub, "get") else None)
        if not sid or sid in seen:
            continue
        seen.add(sid)

        try:
            sub = stripe.Subscription.retrieve(
                str(sid),
                expand=["items.data", "items.data.price"],
            )
        except Exception as e:
            issues.append(f"sub={sid} ERROR retrieve: {e}")
            continue

        ua, cur, price_id = _first_line_price_amount_currency(sub)
        if ua not in target_amounts or cur != target_currency:
            continue

        rows_checked += 1
        cust_id = getattr(sub, "customer", None) or (
            sub.get("customer") if hasattr(sub, "get") else None
        )
        if isinstance(cust_id, dict):
            cust_id = cust_id.get("id")
        if not cust_id:
            issues.append(f"sub={sid} amount=${ua/100:.0f} {cur.upper()} NO_CUSTOMER")
            continue

        try:
            customer = stripe.Customer.retrieve(str(cust_id))
        except Exception as e:
            issues.append(f"sub={sid} customer={cust_id} retrieve failed: {e}")
            continue

        tid_raw = metadata_get(getattr(customer, "metadata", None), "telegram_id")
        if not tid_raw:
            issues.append(
                f"sub={sid} customer={cust_id} email={getattr(customer, 'email', None)!r} "
                f"${ua/100:.0f}/{cur} NO telegram_id in customer metadata"
            )
            continue
        try:
            tid = int(tid_raw)
        except (TypeError, ValueError):
            issues.append(f"sub={sid} bad telegram_id metadata: {tid_raw!r}")
            continue

        cps, cpe = _subscription_period_bounds_unix(sub)
        if cps is None or cpe is None:
            issues.append(
                f"sub={sid} tid={tid} ${ua/100:.0f}/{cur} NO period bounds on subscription (check API)"
            )
            continue

        fs_doc = fs.get_subscription(tid)
        label = f"tid={tid} sub={sid} ${ua/100:.0f}{cur.upper()} price={price_id}"

        if not fs_doc:
            issues.append(f"MISSING_FIRESTORE_DOC {label}")
            continue

        fs_sub = fs_doc.get("stripe_subscription_id")
        if not fs_sub:
            issues.append(
                f"MISSING_FS_stripe_subscription_id {label} "
                f"(stripe_period_end_utc={datetime.fromtimestamp(cpe, tz=timezone.utc).isoformat()})"
            )
        elif str(fs_sub) != str(sid):
            issues.append(
                f"MISMATCH_FS_stripe_subscription_id {label} firestore={fs_sub!r} expected={sid!r}"
            )

        fs_exp = fs_doc.get("expiry_date")
        fs_ts = _dt_to_utc_timestamp(fs_exp)
        if fs_ts is None:
            issues.append(f"MISSING_OR_BAD_FS_expiry_date {label}")
        else:
            delta = abs(fs_ts - cpe)
            if delta > args.tolerance_seconds:
                issues.append(
                    f"EXPIRY_MISMATCH {label} "
                    f"stripe_period_end={datetime.fromtimestamp(cpe, tz=timezone.utc).isoformat()} "
                    f"fs_expiry={fs_exp!r} delta_sec={delta}"
                )
            else:
                ok_n += 1

    print(f"Matched CAD $35/$50 active or trialing subscriptions: {rows_checked}", flush=True)
    print(f"OK (expiry within {args.tolerance_seconds}s, doc exists): {ok_n}", flush=True)
    print(f"Issues: {len(issues)}", flush=True)
    print("", flush=True)
    if issues:
        for line in issues:
            print(line, flush=True)
        print("", flush=True)
        print(
            "Fix: python3 scripts/backfill_firestore_subscription_from_stripe.py "
            "--telegram-id <tid> [--force] --live",
            flush=True,
            )
        print(
            "Canceled-only: add --stripe-subscription-id sub_...",
            flush=True,
        )
        return 1

    print("OK: No mismatches found for CAD $35/$50 active/trialing subscriptions.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
