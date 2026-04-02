#!/usr/bin/env python3
"""
Read-only: print Firestore subscriptions/{telegram_id} and Stripe Customer + subscriptions.

Stripe is the billing source of truth — use this to compare Firestore mirror vs Stripe.

  python3 scripts/inspect_user_billing.py --telegram-id 7908776170

Notes:
  - Flexible-billing subscriptions often expose periods on *items*, not the top-level object.
  - Renewal invoices sit in *draft* briefly (often ~1h) before Stripe finalizes and charges;
    "not paid yet" during that window is normal.

Requires: STRIPE_SECRET_KEY, GOOGLE_CLOUD_PROJECT, ADC with Firestore read access.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

import stripe
from dotenv import load_dotenv

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))


def _item_period_bounds(it0: Any) -> Tuple[Optional[int], Optional[int]]:
    """Period on one subscription item (Flexible Billing often puts periods here only)."""
    try:
        cs = getattr(it0, "current_period_start", None)
        ce = getattr(it0, "current_period_end", None)
        if hasattr(it0, "get"):
            if cs is None:
                cs = it0.get("current_period_start")
            if ce is None:
                ce = it0.get("current_period_end")
        if cs is not None and ce is not None:
            return int(cs), int(ce)
    except (TypeError, ValueError):
        pass
    return None, None


def _subscription_period_bounds_unix(sub: Any) -> Tuple[Optional[int], Optional[int]]:
    """Top-level subscription period, or first subscription item with periods (Flexible Billing)."""
    try:
        cs = getattr(sub, "current_period_start", None)
        ce = getattr(sub, "current_period_end", None)
        if hasattr(sub, "get"):
            if cs is None:
                cs = sub.get("current_period_start")
            if ce is None:
                ce = sub.get("current_period_end")
        if cs is not None and ce is not None:
            return int(cs), int(ce)
    except (TypeError, ValueError):
        pass

    items_obj = getattr(sub, "items", None)
    if items_obj is None and hasattr(sub, "get"):
        try:
            items_obj = sub.get("items")
        except Exception:
            items_obj = None
    data = None
    if items_obj is not None:
        data = getattr(items_obj, "data", None)
        if data is None and hasattr(items_obj, "get"):
            try:
                data = items_obj.get("data")
            except Exception:
                data = None
    if not data:
        return None, None
    best: Tuple[Optional[int], Optional[int]] = (None, None)
    best_end = -1
    for it0 in data:
        try:
            a, b = _item_period_bounds(it0)
            if a is not None and b is not None and b >= best_end:
                best_end = b
                best = (a, b)
        except (TypeError, ValueError, IndexError):
            continue
    if best[0] is not None and best[1] is not None:
        return best
    return None, None


def _invoice_period_fallback(subscription_id: str) -> Tuple[Optional[int], Optional[int], list]:
    """
    If subscription object omits periods, use recent invoices (paid/open/draft) for service window hints.
    Returns (start, end, rows_for_print) where rows are human-readable lines already formatted.
    """
    rows: list = []
    best_start: Optional[int] = None
    best_end: Optional[int] = None
    try:
        invs = stripe.Invoice.list(subscription=subscription_id, limit=15)
    except Exception as e:
        return None, None, [f"    invoice list failed: {e}"]

    for inv in invs.data or []:
        iid = getattr(inv, "id", None)
        st = getattr(inv, "status", None)
        tot = getattr(inv, "total", None)
        try:
            amt = (tot / 100.0) if tot is not None else None
        except Exception:
            amt = None
        ps = getattr(inv, "period_start", None)
        pe = getattr(inv, "period_end", None)
        cr = getattr(inv, "created", None)
        line = "    invoice %s status=%s amount=%s %s period_start=%s period_end=%s created=%s" % (
            iid,
            st,
            amt,
            getattr(inv, "currency", "") or "",
            ps,
            pe,
            cr,
        )
        rows.append(line)
        try:
            if ps is not None and pe is not None:
                ips, ipe = int(ps), int(pe)
                if best_end is None or ipe >= best_end:
                    best_start, best_end = ips, ipe
        except (TypeError, ValueError):
            pass

    return best_start, best_end, rows


def _stripe_object_to_plain_dict(obj) -> dict:
    """
    StripeObject: prefer to_dict(); avoid dict() constructor (can KeyError on StripeObject).
    """
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return {str(k): v for k, v in obj.items()}
    td = getattr(obj, "to_dict", None)
    if callable(td):
        try:
            raw = td()
            if isinstance(raw, dict):
                return {str(k): raw[k] for k in raw.keys()}
        except Exception:
            pass
    try:
        keys = list(obj.keys())
    except Exception:
        return {"_error": repr(obj)}
    out: dict = {}
    for k in keys:
        try:
            out[str(k)] = obj[k]
        except Exception as exc:
            out[str(k)] = f"<unreadable: {exc}>"
    return out


def _json_safe(obj):
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except Exception:
            return str(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    return str(obj)


def main() -> int:
    load_dotenv(_REPO / ".env")
    p = argparse.ArgumentParser(description="Inspect Firestore + Stripe for one Telegram user.")
    p.add_argument("--telegram-id", type=int, required=True)
    args = p.parse_args()

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

    try:
        from google.cloud import firestore
    except ImportError as exc:
        print(f"ERROR: google-cloud-firestore: {exc}", file=sys.stderr)
        return 1

    db = firestore.Client(project=project)
    doc = db.collection("subscriptions").document(str(tid)).get()
    fs_expiry = None
    fs_status = None
    print("=== Firestore subscriptions/%s ===" % tid, flush=True)
    if doc.exists:
        d = doc.to_dict()
        print(json.dumps(_json_safe(d), indent=2), flush=True)
        fs_expiry = d.get("expiry_date")
        fs_status = d.get("status")
    else:
        print("(no document)", flush=True)

    print("", flush=True)
    print("=== Stripe Customer.search metadata['telegram_id'] ===", flush=True)
    res = stripe.Customer.search(query=f"metadata['telegram_id']:'{tid}'", limit=10)
    if not res.data:
        print("(no customer with this telegram_id in metadata)", flush=True)
        return 0

    for i, c in enumerate(res.data):
        cid = c.id
        print("--- customer[%d] %s ---" % (i, cid), flush=True)
        print("  email: %r" % getattr(c, "email", None), flush=True)
        print("  metadata: %r" % _stripe_object_to_plain_dict(getattr(c, "metadata", None)), flush=True)

        for st in ("active", "trialing", "past_due", "canceled", "unpaid"):
            subs = stripe.Subscription.list(customer=cid, status=st, limit=20)
            batch = subs.data or []
            if not batch:
                continue
            print("  subscriptions status=%s (%d):" % (st, len(batch)), flush=True)
            for sub in batch:
                pe = getattr(sub, "current_period_end", None)
                ps = getattr(sub, "current_period_start", None)
                print(
                    "    id=%s items=%s period_start=%s period_end=%s cancel_at_period_end=%s"
                    % (
                        sub.id,
                        getattr(sub, "status", None),
                        ps,
                        pe,
                        getattr(sub, "cancel_at_period_end", None),
                    ),
                    flush=True,
                )
                # List view often omits period fields; retrieve full subscription for ground truth.
                try:
                    full = stripe.Subscription.retrieve(
                        sub.id,
                        expand=[
                            "items.data",
                            "items.data.price",
                            "latest_invoice",
                        ],
                    )
                except Exception as e:
                    print("    retrieve failed: %s" % e, flush=True)
                    continue
                bm = getattr(full, "billing_mode", None)
                if bm is not None:
                    print(
                        "    billing_mode: %r"
                        % (_stripe_object_to_plain_dict(bm) if bm is not None else None),
                        flush=True,
                    )
                cps_u, cpe_u = _subscription_period_bounds_unix(full)
                now_ts = int(datetime.now(timezone.utc).timestamp())
                print("    --- retrieve(%s) ---" % sub.id, flush=True)
                if cps_u is None or cpe_u is None:
                    inv_s, inv_e, inv_rows = _invoice_period_fallback(sub.id)
                    print(
                        "    (subscription object had no period bounds — trying invoices)",
                        flush=True,
                    )
                    for r in inv_rows:
                        print(r, flush=True)
                    if inv_s is not None and inv_e is not None:
                        cps_u, cpe_u = inv_s, inv_e
                        print(
                            "    → using best invoice period_start/end as fallback: %s .. %s"
                            % (inv_s, inv_e),
                            flush=True,
                        )
                if cps_u is not None and cpe_u is not None:
                    cps_dt = datetime.fromtimestamp(cps_u, tz=timezone.utc)
                    cpe_dt = datetime.fromtimestamp(cpe_u, tz=timezone.utc)
                    print(
                        "    current_period_start: %s" % cps_dt.isoformat(),
                        flush=True,
                    )
                    print(
                        "    current_period_end:   %s" % cpe_dt.isoformat(),
                        flush=True,
                    )
                    if cpe_u < now_ts:
                        print(
                            "    vs now UTC: period END is in the PAST — Stripe still shows "
                            "status=%r on this object (renewal may be pending, or sub stuck active)."
                            % getattr(full, "status", None),
                            flush=True,
                        )
                    else:
                        print(
                            "    vs now UTC: period END is in the FUTURE — Stripe considers "
                            "this billing period still current.",
                            flush=True,
                        )
                    if fs_expiry is not None:
                        fs_e = fs_expiry
                        if hasattr(fs_e, "timestamp"):
                            fs_ts = int(fs_e.timestamp())
                        else:
                            fs_ts = None
                        if fs_ts is not None:
                            if cpe_u < fs_ts - 60:
                                print(
                                    "    vs Firestore expiry_date: Stripe period_end is EARLIER than "
                                    "Firestore — unusual; check clocks/webhooks.",
                                    flush=True,
                            )
                            elif cpe_u > fs_ts + 60:
                                print(
                                    "    vs Firestore expiry_date: Stripe period_end is LATER than "
                                    "Firestore — Firestore likely stale (e.g. missed renewal webhook).",
                                    flush=True,
                            )
                            else:
                                print(
                                    "    vs Firestore expiry_date: roughly aligned (same ballpark).",
                                    flush=True,
                            )
                else:
                    print(
                        "    current_period_start/end: still unknown after subscription + invoices — "
                        "open this subscription in Stripe Dashboard.",
                        flush=True,
                    )

    print("", flush=True)
    print("=== Interpretation ===", flush=True)
    print(
        "Firestore status=%r drives /status and kicks; Stripe is billing source of truth."
        % (fs_status,),
        flush=True,
    )
    print(
        "If Stripe period_end is AFTER Firestore expiry_date, sync Firestore from Stripe "
        "(webhooks or scripts/sync_firestore_subscription_from_stripe.py) — do not assume "
        "Firestore-only expiry is correct.",
        flush=True,
    )
    print(
        "Draft invoices: Stripe often creates the renewal invoice in draft first, then "
        "finalizes and charges within about an hour — not paid immediately at period rollover.",
        flush=True,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
