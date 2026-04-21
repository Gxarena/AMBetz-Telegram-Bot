#!/usr/bin/env python3
"""
Read-only: for every row in `find_expired_subscriptions()`, check Stripe for an
*entitled* subscription (active or trialing with current period end in the future),
matching the same idea as `try_refresh_firestore_mirror_from_stripe` in production.

- OK     — no such Stripe cover (kicking would not be blocked by Stripe sync).
- STILL  — at least one sub looks entitled in Stripe (the real /check-expired would
           refresh Firestore and skip the kick for this person).

Usage (repo root):
  python3 scripts/audit_stripe_for_expired_queue.py

Requires: GOOGLE_CLOUD_PROJECT, ADC; STRIPE_SECRET_KEY (or dotenv with Stripe key).
Does not change Firestore or Stripe.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

import stripe
from dotenv import load_dotenv

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from firestore_service import FirestoreService  # noqa: E402
from gcp_stripe_service import _subscription_period_bounds_unix  # noqa: E402


def _customer_id_for_telegram(telegram_id: int, doc: Optional[dict]) -> Optional[str]:
    if doc and doc.get("stripe_customer_id"):
        return str(doc["stripe_customer_id"])
    res = stripe.Customer.search(query=f"metadata['telegram_id']:'{telegram_id}'", limit=5)
    if not res.data:
        return None
    return res.data[0].id


def _has_future_entitlement(customer_id: str) -> Tuple[bool, str]:
    """True if any active/trialing sub has period end strictly in the future."""
    now = int(time.time())
    bits: List[str] = []
    for status in ("active", "trialing"):
        page = stripe.Subscription.list(customer=customer_id, status=status, limit=100)
        for sub in page.auto_paging_iter():
            _, pe = _subscription_period_bounds_unix(sub)
            st = getattr(sub, "id", "?")
            stat = getattr(sub, "status", "?")
            if pe is None:
                bits.append(f"{st}:{stat}:no_period")
                continue
            end_iso = datetime.fromtimestamp(pe, tz=timezone.utc).isoformat()
            if pe > now:
                return True, f"YES {st} status={stat} period_end={end_iso} (still entitled)"
            bits.append(f"{st}:{stat} period_end={end_iso} (past)")
    if not bits:
        return False, "no active/trialing subs"
    return False, "; ".join(bits)[:200]


def main() -> None:
    load_dotenv(_REPO / ".env")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    sk = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
    if not project:
        print("Set GOOGLE_CLOUD_PROJECT.", file=sys.stderr)
        sys.exit(1)
    if not sk:
        print("Set STRIPE_SECRET_KEY.", file=sys.stderr)
        sys.exit(1)
    stripe.api_key = sk

    fs = FirestoreService(project_id=project)
    rows = fs.find_expired_subscriptions()
    if not rows:
        print("Empty find_expired_subscriptions() — nothing to check.")
        return

    still: List[Tuple[int, str]] = []
    ok: List[int] = []
    for s in sorted(rows, key=lambda x: (x.get("expiry_date") or "")):
        tid = int(s.get("telegram_id", 0))
        doc = fs.get_subscription(tid)
        cust = _customer_id_for_telegram(tid, doc)
        if not cust:
            ok.append(tid)
            print(f"OK  telegram_id={tid}  (no Stripe customer for this id)")
            continue
        ent, detail = _has_future_entitlement(cust)
        if ent:
            still.append((tid, detail))
            print(f"STILL  telegram_id={tid}  customer={cust}  {detail}")
        else:
            ok.append(tid)
            print(f"OK  telegram_id={tid}  customer={cust}  {detail}")

    print()
    print("---")
    print(f"Total: {len(rows)}  /  No future Stripe period (ok to treat as lapsed in Stripe): {len(ok)}")
    print(f"Has at least one active/trialing sub with future period end: {len(still)}")
    if still:
        print("\nThese users would be **skipped** on /check-expired (Firestore refreshed from Stripe), not kicked:")
        for tid, d in still:
            print(f"  {tid}  {d}")
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
