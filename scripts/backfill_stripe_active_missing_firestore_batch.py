#!/usr/bin/env python3
"""
For every Stripe *active* or *trialing* subscription that has NO Firestore document
indexed by that stripe_subscription_id, resolve Telegram id and run
backfill_firestore_subscription_from_stripe.py (dry-run unless --live).

Telegram id resolution:
  1) Customer.metadata.telegram_id
  2) Else users/ by email (lowercased) via get_user_by_email

If several missing subscriptions resolve to the same telegram_id (duplicate
Stripe customers for one person), only the subscription with the latest
current_period_end is queued; others are listed for manual cancel in Stripe.

Usage (repo root):
  python3 scripts/backfill_stripe_active_missing_firestore_batch.py
  python3 scripts/backfill_stripe_active_missing_firestore_batch.py --live
  python3 scripts/backfill_stripe_active_missing_firestore_batch.py --live --force
  python3 scripts/backfill_stripe_active_missing_firestore_batch.py --firestore-project ID

Requires: STRIPE_SECRET_KEY, GOOGLE_CLOUD_PROJECT, ADC for Firestore.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, DefaultDict, Dict, List, Optional, Tuple

import stripe
from dotenv import load_dotenv

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from gcp_stripe_service import _subscription_period_bounds_unix  # noqa: E402
from stripe_compat import metadata_get  # noqa: E402


def _list_live_subscriptions() -> List[Any]:
    out: List[Any] = []
    for status in ("active", "trialing"):
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


def _sub_id(sub: Any) -> Optional[str]:
    sid = getattr(sub, "id", None)
    if sid:
        return str(sid)
    if hasattr(sub, "get"):
        v = sub.get("id")
        return str(v) if v else None
    return None


def _cust_obj(sub: Any) -> Any:
    raw = getattr(sub, "customer", None) if not hasattr(sub, "get") else sub.get("customer")
    if isinstance(raw, str):
        try:
            return stripe.Customer.retrieve(raw)
        except Exception:
            return None
    return raw


def _cust_id_of(sub: Any) -> Optional[str]:
    raw = getattr(sub, "customer", None) if not hasattr(sub, "get") else sub.get("customer")
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return str(raw.get("id") or "") or None
    cid = getattr(raw, "id", None)
    return str(cid) if cid else None


def _email_tid(cust: Any) -> Tuple[Optional[str], Optional[str]]:
    if cust is None:
        return None, None
    email = getattr(cust, "email", None) if not hasattr(cust, "get") else cust.get("email")
    em = str(email).strip().lower() if email else None
    meta = getattr(cust, "metadata", None) if not hasattr(cust, "get") else cust.get("metadata")
    tid = metadata_get(meta, "telegram_id")
    tid_s = str(tid).strip() if tid is not None and str(tid).strip() else None
    return em, tid_s


def _period_end_unix(sub_id: str) -> int:
    try:
        sub = stripe.Subscription.retrieve(
            sub_id,
            expand=["items.data", "items.data.price", "latest_invoice"],
        )
    except Exception:
        return 0
    _, pe = _subscription_period_bounds_unix(sub)
    return int(pe) if pe is not None else 0


def main() -> int:
    load_dotenv(_REPO / ".env")
    p = argparse.ArgumentParser(
        description="Backfill Firestore for Stripe live subs missing stripe_subscription_id index."
    )
    p.add_argument("--live", action="store_true", help="Write Firestore via backfill script.")
    p.add_argument(
        "--force",
        action="store_true",
        help="Pass --force to each backfill (needed if subscriptions/{tid} is already status=active).",
    )
    p.add_argument("--firestore-project", default=None, metavar="ID", help="Override GOOGLE_CLOUD_PROJECT.")
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

    from firestore_service import FirestoreService  # noqa: E402

    try:
        fs = FirestoreService(project_id=project)
    except Exception as exc:
        print(f"ERROR: Firestore init failed: {exc}", file=sys.stderr)
        return 1

    stubs = _list_live_subscriptions()
    missing_raw: List[Dict[str, Any]] = []

    for sub in stubs:
        sid = _sub_id(sub)
        if not sid:
            continue
        if fs.get_subscription_by_stripe_subscription_id(sid):
            continue

        cust = _cust_obj(sub)
        em, meta_tid = _email_tid(cust)
        cid = _cust_id_of(sub)
        tid: Optional[int] = None
        if meta_tid:
            try:
                tid = int(meta_tid)
            except ValueError:
                pass
        if tid is None and em:
            u = fs.get_user_by_email(em)
            if u and u.get("telegram_id") is not None:
                try:
                    tid = int(u["telegram_id"])
                except (TypeError, ValueError):
                    tid = None

        st = getattr(sub, "status", None) or (sub.get("status") if hasattr(sub, "get") else None)
        missing_raw.append(
            {
                "stripe_subscription_id": sid,
                "stripe_status": st,
                "customer_id": cid,
                "email": em,
                "telegram_id": tid,
                "source_tid": "metadata" if meta_tid else ("email" if tid is not None else None),
            }
        )

    by_tid: DefaultDict[int, List[Dict[str, Any]]] = defaultdict(list)
    unresolved: List[Dict[str, Any]] = []
    for row in missing_raw:
        tid = row["telegram_id"]
        if tid is None:
            unresolved.append(row)
        else:
            by_tid[int(tid)].append(row)

    to_run: List[Tuple[int, str, List[str]]] = []
    # (telegram_id, winning_sub_id, skipped_sub_ids)
    for tid, rows in sorted(by_tid.items()):
        if len(rows) == 1:
            to_run.append((tid, rows[0]["stripe_subscription_id"], []))
            continue
        ranked = sorted(rows, key=lambda r: _period_end_unix(r["stripe_subscription_id"]), reverse=True)
        winner = ranked[0]["stripe_subscription_id"]
        skipped = [r["stripe_subscription_id"] for r in ranked[1:]]
        to_run.append((tid, winner, skipped))

    print(f"Stripe live subscriptions scanned: {len(stubs)}", flush=True)
    print(f"Missing Firestore stripe_subscription_id index: {len(missing_raw)}", flush=True)
    print(f"Unresolved (no telegram_id and no users/ email match): {len(unresolved)}", flush=True)
    print(f"Backfill jobs after dedupe by telegram_id: {len(to_run)}", flush=True)
    print("", flush=True)

    for row in unresolved:
        print(
            f"SKIP_UNRESOLVED sub={row['stripe_subscription_id']} customer={row['customer_id']} "
            f"email={row['email']!r} — set Customer metadata or add email on users/",
            flush=True,
        )
    if unresolved:
        print("", flush=True)

    for tid, winner_sid, skipped in to_run:
        print(f"telegram_id={tid} → backfill sub={winner_sid}", flush=True)
        for o in skipped:
            print(
                f"  NOTE: also had missing index sub={o} (skipped; cancel duplicate in Stripe if same person)",
                flush=True,
            )

    if not to_run:
        print("Nothing to backfill.", flush=True)
        return 0

    print("", flush=True)
    if not args.live:
        print("DRY-RUN: no writes. Pass --live to run backfill_firestore_subscription_from_stripe.py for each row.", flush=True)
        return 0

    backfill_py = _REPO / "scripts" / "backfill_firestore_subscription_from_stripe.py"
    failed = 0
    for tid, winner_sid, skipped in to_run:
        cmd = [
            sys.executable,
            str(backfill_py),
            "--telegram-id",
            str(tid),
            "--stripe-subscription-id",
            winner_sid,
            "--live",
        ]
        if args.force:
            cmd.append("--force")
        print(f"\n--- running: {' '.join(cmd)} ---", flush=True)
        r = subprocess.run(cmd, cwd=str(_REPO))
        if r.returncode != 0:
            failed += 1
            print(
                f"ERROR: backfill exited {r.returncode} for tid={tid} sub={winner_sid}. "
                f"If Firestore already active, re-run this batch with --force.",
                file=sys.stderr,
                flush=True,
            )

    if args.live and any(s[2] for s in to_run):
        print(
            "\nSome telegram_ids had multiple missing Stripe subs; only the latest period was backfilled. "
            "Cancel duplicate subscriptions in Stripe if they bill the same person.",
            flush=True,
        )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
