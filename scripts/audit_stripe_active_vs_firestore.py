#!/usr/bin/env python3
"""
Read-only: Stripe (active + trialing subscriptions) as source of truth vs Firestore.

For each live Stripe subscription, checks subscriptions/* via
get_subscription_by_stripe_subscription_id(sub.id).

Categories:
  OK              — Firestore doc exists; stripe_customer_id matches Stripe customer.
  MISSING         — No Firestore document with this stripe_subscription_id.
  MISMATCH_CUSTOMER — Doc exists but stripe_customer_id differs from Stripe subscription.customer.
  WARN_STATUS     — Doc exists and customer matches, but Firestore status is not active/trialing
                    (informational; you may use other statuses for trials).

Optional --firestore-extras: list Firestore subscription docs that look still “live”
(status active/trialing) but whose stripe_subscription_id is not in the Stripe live set
(stale or missing Stripe id on the doc).

Usage (repo root):
  python3 scripts/audit_stripe_active_vs_firestore.py
  python3 scripts/audit_stripe_active_vs_firestore.py --verbose
  python3 scripts/audit_stripe_active_vs_firestore.py --firestore-extras
  python3 scripts/audit_stripe_active_vs_firestore.py --firestore-project my-project

Requires: STRIPE_SECRET_KEY, GOOGLE_CLOUD_PROJECT (or --firestore-project), ADC with Firestore read.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, DefaultDict, Dict, List, Optional, Set, Tuple

import stripe
from dotenv import load_dotenv

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from stripe_compat import metadata_get  # noqa: E402


def _list_subscriptions_paginated(status: str) -> List[Any]:
    out: List[Any] = []
    params: Dict[str, Any] = {
        "status": status,
        "limit": 100,
        "expand": ["data.customer"],
    }
    while True:
        page = stripe.Subscription.list(**params)
        batch = page.data or []
        out.extend(batch)
        if not getattr(page, "has_more", False) or not batch:
            break
        params["starting_after"] = batch[-1].id
    return out


def _customer_id(sub: Any) -> Optional[str]:
    raw = getattr(sub, "customer", None) if not hasattr(sub, "get") else sub.get("customer")
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return str(raw.get("id") or "")
    cid = getattr(raw, "id", None)
    return str(cid) if cid else None


def _customer_email_and_telegram(customer_obj: Any) -> Tuple[Optional[str], Optional[str]]:
    if customer_obj is None:
        return None, None
    email = getattr(customer_obj, "email", None) if not hasattr(customer_obj, "get") else customer_obj.get("email")
    meta = getattr(customer_obj, "metadata", None) if not hasattr(customer_obj, "get") else customer_obj.get("metadata")
    tid = metadata_get(meta, "telegram_id")
    tid_s = str(tid).strip() if tid is not None and str(tid).strip() else None
    em = str(email).strip() if email else None
    return em, tid_s


def main() -> int:
    load_dotenv(_REPO / ".env")
    p = argparse.ArgumentParser(
        description="Audit Firestore vs Stripe active/trialing subscriptions (Stripe is source of truth)."
    )
    p.add_argument(
        "--firestore-project",
        metavar="ID",
        default=None,
        help="Override GOOGLE_CLOUD_PROJECT.",
    )
    p.add_argument(
        "--firestore-extras",
        action="store_true",
        help="Also list Firestore rows that look live but sub id not in Stripe live set.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print every OK line (default: summary counts only for OK).",
    )
    args = p.parse_args()

    secret = os.environ.get("STRIPE_SECRET_KEY")
    project = args.firestore_project or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not secret:
        print("ERROR: STRIPE_SECRET_KEY not set.", file=sys.stderr)
        return 1
    if not project:
        print("ERROR: GOOGLE_CLOUD_PROJECT or --firestore-project required.", file=sys.stderr)
        return 1

    stripe.api_key = secret

    from firestore_service import FirestoreService

    try:
        fs = FirestoreService(project_id=project)
    except Exception as exc:
        print(f"ERROR: Firestore init failed: {exc}", file=sys.stderr)
        return 1

    stubs: List[Any] = []
    for st in ("active", "trialing"):
        stubs.extend(_list_subscriptions_paginated(st))

    live_ids: Set[str] = set()
    ok: List[str] = []
    missing: List[str] = []
    mismatch_customer: List[str] = []
    warn_status: List[str] = []
    warn_telegram: List[str] = []

    print(f"Stripe live subscriptions (active + trialing, with data.customer expanded): {len(stubs)}", flush=True)
    print("", flush=True)

    for sub in stubs:
        sid = getattr(sub, "id", None) or (sub.get("id") if hasattr(sub, "get") else None)
        if not sid:
            continue
        live_ids.add(str(sid))
        st = getattr(sub, "status", None) or (sub.get("status") if hasattr(sub, "get") else None)
        cust_id = _customer_id(sub)
        cust_obj = getattr(sub, "customer", None) if not hasattr(sub, "get") else sub.get("customer")
        if isinstance(cust_obj, str):
            try:
                cust_obj = stripe.Customer.retrieve(cust_obj)
            except Exception:
                cust_obj = None
        email, stripe_meta_tid = _customer_email_and_telegram(cust_obj)

        base = (
            f"sub={sid} stripe_status={st} customer={cust_id or '(none)'} "
            f"email={email!r} customer.metadata.telegram_id={stripe_meta_tid!r}"
        )

        if not cust_id:
            missing.append(f"MISSING_STRIPE_CUSTOMER {base}")
            continue

        doc = fs.get_subscription_by_stripe_subscription_id(str(sid))
        if not doc:
            missing.append(f"MISSING_FIRESTORE {base}")
            continue

        fs_tid = doc.get("telegram_id")
        doc_cust = doc.get("stripe_customer_id")
        doc_st = (doc.get("status") or "").lower()

        if str(doc_cust or "") != str(cust_id):
            mismatch_customer.append(
                f"MISMATCH_CUSTOMER {base} "
                f"firestore.telegram_id={fs_tid} firestore.stripe_customer_id={doc_cust!r}"
            )
            continue

        if stripe_meta_tid and fs_tid is not None and str(fs_tid) != str(stripe_meta_tid):
            warn_telegram.append(
                f"WARN_TELEGRAM_META {base} firestore.doc_telegram_id={fs_tid}"
            )

        if doc_st and doc_st not in ("active", "trialing"):
            warn_status.append(
                f"WARN_STATUS {base} firestore.telegram_id={fs_tid} firestore.status={doc_st!r}"
            )
        else:
            ok.append(f"OK {base} firestore.telegram_id={fs_tid}")

    counts: DefaultDict[str, int] = defaultdict(int)
    counts["ok"] = len(ok)
    counts["missing_or_bad_stripe"] = sum(1 for x in missing if x.startswith("MISSING_STRIPE"))
    counts["missing_firestore"] = sum(1 for x in missing if x.startswith("MISSING_FIRESTORE"))
    counts["mismatch_customer"] = len(mismatch_customer)
    counts["warn_status"] = len(warn_status)
    counts["warn_telegram"] = len(warn_telegram)

    print("=== SUMMARY (Stripe → Firestore) ===", flush=True)
    for k in (
        "ok",
        "missing_firestore",
        "missing_or_bad_stripe",
        "mismatch_customer",
        "warn_status",
        "warn_telegram",
    ):
        print(f"  {k}: {counts[k]}", flush=True)
    print("", flush=True)

    def _block(title: str, lines: List[str]) -> None:
        if not lines:
            return
        print(f"=== {title} ({len(lines)}) ===", flush=True)
        for line in lines:
            print(line, flush=True)
        print("", flush=True)

    _block("MISSING_OR_STRIPE_INCOMPLETE", [x for x in missing if x.startswith("MISSING_STRIPE")])
    _block("MISSING_FIRESTORE (no doc for this stripe_subscription_id)", [x for x in missing if x.startswith("MISSING_FIRESTORE")])
    _block("MISMATCH_CUSTOMER", mismatch_customer)
    _block("WARN_STATUS", warn_status)
    _block("WARN_TELEGRAM_META (Stripe metadata vs Firestore doc id)", warn_telegram)

    if ok and args.verbose:
        print(f"=== OK ({len(ok)}) ===", flush=True)
        for line in ok:
            print(line, flush=True)
        print("", flush=True)
    elif ok and not args.verbose:
        print(f"(OK rows omitted; {len(ok)} aligned. Use --verbose to print each.)", flush=True)
        print("", flush=True)

    if args.firestore_extras:
        extras: List[str] = []
        try:
            for doc_snap in fs.db.collection("subscriptions").stream():
                d = doc_snap.to_dict() or {}
                chat_id = doc_snap.id
                raw_st = (d.get("status") or "").lower()
                if raw_st not in ("active", "trialing"):
                    continue
                fs_sub_id = d.get("stripe_subscription_id")
                if not fs_sub_id or not str(fs_sub_id).strip():
                    extras.append(
                        f"EXTRA_FIRESTORE_NO_SUB_ID telegram_id={chat_id} status={d.get('status')!r} "
                        f"stripe_customer_id={d.get('stripe_customer_id')!r}"
                    )
                    continue
                if str(fs_sub_id) not in live_ids:
                    extras.append(
                        f"EXTRA_FIRESTORE_STALE_SUB telegram_id={chat_id} status={d.get('status')!r} "
                        f"stripe_subscription_id={fs_sub_id!r} stripe_customer_id={d.get('stripe_customer_id')!r}"
                    )
        except Exception as exc:
            print(f"ERROR: Firestore scan failed: {exc}", file=sys.stderr)
            return 1

        print(f"=== FIRESTORE EXTRAS (docs active/trialing but sub not in Stripe live set) ({len(extras)}) ===", flush=True)
        for line in extras:
            print(line, flush=True)
        print("", flush=True)

    bad = counts["missing_firestore"] + counts["missing_or_bad_stripe"] + counts["mismatch_customer"]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
