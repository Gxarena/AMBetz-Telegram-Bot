#!/usr/bin/env python3
"""
Read-only: find Stripe Customers that share the same email (duplicate rows).

Stripe has one Customer object per id; nothing stops the same person from getting
multiple Customer records if checkouts or integrations create a new customer
instead of reusing the existing one (missing customer= on Session, dashboard
"create customer", old flows without metadata, etc.).

Why duplicate rows sometimes have no metadata.telegram_id:
  - **Historical / non-bot checkouts**: older code, Dashboard-created Customers, or Sessions that
    did not reuse `customer=` so Stripe created a new Customer from email.
  - **Legacy Payment Link** (only if you ever used it): `create_payment_link` in code does not
    attach `customer=` to the link; there are no current call sites in this repo, but old links
    or past deploys could still explain some rows.

Why the same email can show two different telegram_id values on two Customer objects:
  - Usually a data mistake: family/shared email, account takeover, wrong metadata copied,
    or the same email used by two Telegram users in different checkouts. Do not merge until
    you know which Stripe customer belongs to which person.

Usage (repo root):
  python3 scripts/audit_stripe_duplicate_customers_by_email.py
  python3 scripts/audit_stripe_duplicate_customers_by_email.py --live-subs
  python3 scripts/audit_stripe_duplicate_customers_by_email.py --min-count 2
  python3 scripts/audit_stripe_duplicate_customers_by_email.py --firestore

Requires: STRIPE_SECRET_KEY; GOOGLE_CLOUD_PROJECT (+ ADC) when using --firestore
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, DefaultDict, Dict, List, Optional, Tuple

import stripe
from dotenv import load_dotenv

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from stripe_compat import metadata_get  # noqa: E402


def _pick(d: Optional[Dict[str, Any]], *keys: str) -> str:
    if not d:
        return "—"
    for k in keys:
        v = d.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return "—"


def _firestore_subscription_line(sub: Dict[str, Any]) -> str:
    return (
        f"sub.telegram_id={sub.get('telegram_id')} "
        f"stripe_customer_id={_pick(sub, 'stripe_customer_id')} "
        f"stripe_subscription_id={_pick(sub, 'stripe_subscription_id')} "
        f"status={_pick(sub, 'status')} "
        f"expiry={_pick(sub, 'expiry_date', 'expires_at')}"
    )


def _firestore_user_line(u: Dict[str, Any]) -> str:
    return (
        f"user.telegram_id(doc)={u.get('telegram_id')} "
        f"username={_pick(u, 'username')} "
        f"first_name={_pick(u, 'first_name')} "
        f"last_name={_pick(u, 'last_name')} "
        f"email={_pick(u, 'email')}"
    )


def _safe_email(c: Any) -> str:
    e = getattr(c, "email", None)
    if not e or not str(e).strip():
        return ""
    return str(e).strip().lower()


def _live_subscriptions_line(customer_id: str) -> str:
    """Short summary of active + trialing subscriptions for this customer."""
    ids: List[str] = []
    for st in ("active", "trialing"):
        try:
            page = stripe.Subscription.list(customer=customer_id, status=st, limit=20)
        except Exception as exc:
            return f"(list failed: {exc})"
        for s in page.data or []:
            sid = getattr(s, "id", None) or ""
            if sid:
                ids.append(f"{sid}:{st}")
    if not ids:
        return "no active/trialing subscriptions"
    return "; ".join(ids)


def main() -> int:
    load_dotenv(_REPO / ".env")
    p = argparse.ArgumentParser(description="List Stripe customer ids grouped by email (duplicates).")
    p.add_argument(
        "--min-count",
        type=int,
        default=2,
        metavar="N",
        help="Only print emails with at least N customers (default 2).",
    )
    p.add_argument(
        "--max-customers",
        type=int,
        default=0,
        help="Stop after fetching this many customers (0 = no limit; full scan).",
    )
    p.add_argument(
        "--live-subs",
        action="store_true",
        help="For each duplicate-email row, list Stripe subscriptions with status active or trialing.",
    )
    p.add_argument(
        "--firestore",
        action="store_true",
        help="Cross-check Firestore users (by email) and subscriptions (by stripe_customer_id).",
    )
    p.add_argument(
        "--firestore-project",
        metavar="ID",
        default=None,
        help="Override GOOGLE_CLOUD_PROJECT for Firestore (default: env).",
    )
    args = p.parse_args()

    secret = os.environ.get("STRIPE_SECRET_KEY")
    if not secret:
        print("ERROR: STRIPE_SECRET_KEY not set.", file=sys.stderr)
        return 1
    stripe.api_key = secret

    fs: Optional[Any] = None
    if args.firestore:
        from firestore_service import FirestoreService

        proj = args.firestore_project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not proj:
            print("ERROR: GOOGLE_CLOUD_PROJECT or --firestore-project required with --firestore.", file=sys.stderr)
            return 1
        try:
            fs = FirestoreService(project_id=proj)
        except Exception as exc:
            print(f"ERROR: Firestore init failed: {exc}", file=sys.stderr)
            return 1

    by_email: DefaultDict[str, List[Tuple[str, str, str]]] = defaultdict(list)
    # tuple: (customer_id, created_iso_or_?, telegram_meta)

    fetched = 0
    starting_after: Optional[str] = None
    while True:
        params: Dict[str, Any] = {"limit": 100}
        if starting_after:
            params["starting_after"] = starting_after
        try:
            page = stripe.Customer.list(**params)
        except Exception as exc:
            print(f"ERROR: Customer.list failed: {exc}", file=sys.stderr)
            return 1
        batch = page.data or []
        for c in batch:
            fetched += 1
            em = _safe_email(c)
            if not em:
                continue
            cid = getattr(c, "id", None) or ""
            cr = getattr(c, "created", None)
            created_s = str(cr) if cr is not None else "?"
            tid = metadata_get(getattr(c, "metadata", None), "telegram_id")
            tid_s = str(tid) if tid is not None and str(tid).strip() else "(no telegram_id)"
            by_email[em].append((cid, created_s, tid_s))

        if args.max_customers and fetched >= args.max_customers:
            break
        if not getattr(page, "has_more", False) or not batch:
            break
        starting_after = batch[-1].id

    dups = [(em, rows) for em, rows in by_email.items() if len(rows) >= args.min_count]
    dups.sort(key=lambda x: (-len(x[1]), x[0]))

    print(f"Fetched {fetched} customers (with pagination).", flush=True)
    print(
        f"Unique emails (non-empty): {len(by_email)}; emails with >= {args.min_count} customers: {len(dups)}",
        flush=True,
    )
    print("", flush=True)

    if not dups:
        print("OK: No duplicate emails in this Stripe account (for scanned customers).", flush=True)
        return 0

    for em, rows in dups:
        tids = {t for _, _, t in rows if t and not str(t).startswith("(no")}
        if len(tids) >= 2:
            warn = "  WARNING: multiple distinct telegram_id values on this email — review before merging"
        else:
            warn = ""
        print(f"=== {em!r}  ({len(rows)} customers) ===", flush=True)
        if warn:
            print(warn, flush=True)
        if fs is not None:
            try:
                u_email = fs.get_user_by_email(em)
            except Exception as exc:
                u_email = None
                print(f"  Firestore get_user_by_email failed: {exc}", flush=True)
            if u_email:
                print(f"  Firestore users (match email): {_firestore_user_line(u_email)}", flush=True)
            else:
                print("  Firestore users (match email): (no document with this email)", flush=True)
        for cid, created, tid in sorted(rows, key=lambda r: int(r[1]) if r[1].isdigit() else 0):
            line = f"  {cid}  created={created}  metadata.telegram_id={tid}"
            if args.live_subs:
                line += f"  |  {_live_subscriptions_line(cid)}"
            print(line, flush=True)
            if fs is not None:
                try:
                    sub_doc = fs.get_subscription_by_stripe_customer(cid)
                except Exception as exc:
                    print(f"      Firestore subscriptions: (query failed: {exc})", flush=True)
                    sub_doc = None
                if sub_doc:
                    print(f"      Firestore subscriptions: {_firestore_subscription_line(sub_doc)}", flush=True)
                else:
                    print("      Firestore subscriptions: (no doc with stripe_customer_id)", flush=True)
                tid_clean = tid.replace("(no telegram_id)", "").strip()
                if tid_clean.isdigit():
                    try:
                        u_tg = fs.get_user(int(tid_clean))
                    except Exception:
                        u_tg = None
                    if u_tg:
                        print(f"      Firestore users (Stripe metadata telegram_id): {_firestore_user_line(u_tg)}", flush=True)
                    else:
                        print(
                            f"      Firestore users (metadata telegram_id={tid_clean}): no users/{tid_clean} document",
                            flush=True,
                        )
        print("", flush=True)

    print(
        "Ops: triage duplicate Customers manually; Stripe rarely offers a one-click “merge” for arbitrary duplicates. "
        "Prefer reusing one cus_ with bot metadata and canceling/moving subs as needed.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
