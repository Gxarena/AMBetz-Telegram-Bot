#!/usr/bin/env python3
"""
Migrate existing Stripe subscriptions from OLD_PRICE_ID to NEW_PRICE_ID in place.

Safety defaults:
  - Dry-run unless --live is passed.
  - Live updates use stripe.SubscriptionItem.modify on the matching line item only
    (not Subscription.modify), with proration_behavior=none (no immediate proration charge).
  - Only active/trialing subscriptions; only the subscription item(s) matching the old price.

Diagnose (read-only):
  - Use --diagnose to list every subscription that has OLD_PRICE_ID on a line item, every status,
    and whether the migrator would touch it. Use this to reconcile Dashboard counts vs the script.
  - For status=past_due only: resolves telegram_id / username via Stripe Customer metadata, then
    Firestore (subscriptions by stripe_customer_id, users/{telegram_id}) if GOOGLE_CLOUD_PROJECT is set.
  - If Firestore is available: every row includes the subscription id stored in Firestore for that
    Stripe customer and whether this Stripe subscription is that same id (exposes duplicate/zombie
    subs: e.g. past_due old sub while Firestore tracks the active replacement).
    Also logs current period end (from subscription or subscription items), latest-invoice timing
    (due_date if set; else invoice period_end / effective_at / created), and last successful payment —
    all formatted in US Eastern time (America/New_York, i.e. EST or EDT depending on date).

Run from repo root so `python-dotenv` can load `.env` if present.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import stripe
from dotenv import load_dotenv

# US Eastern for display (Stripe stores Unix times in UTC). Uses EST/EDT correctly.
_EASTERN = ZoneInfo("America/New_York")


def _unix_to_eastern_str(ts: Any) -> str:
    """Format Stripe Unix timestamp (seconds) as local US Eastern date-time string."""
    if ts is None:
        return ""
    try:
        sec = int(ts)
    except (TypeError, ValueError):
        return ""
    if sec <= 0:
        return ""
    dt_utc = datetime.fromtimestamp(sec, tz=timezone.utc)
    dt_e = dt_utc.astimezone(_EASTERN)
    return dt_e.strftime("%Y-%m-%d %I:%M %p %Z")


def _invoice_paid_at_unix(inv: Any) -> Optional[int]:
    st = getattr(inv, "status_transitions", None)
    if st is None:
        return None
    if isinstance(st, dict):
        v = st.get("paid_at")
    else:
        v = getattr(st, "paid_at", None)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _stripe_object_int(obj: Any, key: str) -> Optional[int]:
    """Read int Unix field from StripeObject or dict; None if missing or invalid."""
    v = getattr(obj, key, None)
    if v is None:
        try:
            v = obj[key]
        except (KeyError, TypeError):
            return None
    try:
        i = int(v)
        return i if i > 0 else None
    except (TypeError, ValueError):
        return None


def _subscription_period_end_unix(sub: Any) -> Optional[int]:
    """
    End of current billing period. Stripe often omits top-level subscription.current_period_end
    now; use subscription item(s) current_period_end (max if multiple items).
    """
    top = _stripe_object_int(sub, "current_period_end")
    if top is not None:
        return top
    items_obj = getattr(sub, "items", None)
    data = getattr(items_obj, "data", None) if items_obj is not None else None
    if not data:
        return None
    best: Optional[int] = None
    for it in data:
        pe = _stripe_object_int(it, "current_period_end")
        if pe is not None:
            best = pe if best is None else max(best, pe)
    return best


def _invoice_best_due_or_period_unix(inv: Any) -> Optional[int]:
    """
    When payment was / is owed for this invoice. due_date is often null for charge_automatically;
    then use period_end (billing period the invoice covers), then effective_at, then created.
    """
    for key in ("due_date", "period_end", "effective_at", "created"):
        t = _stripe_object_int(inv, key)
        if t is not None:
            return t
    return None


def _past_due_billing_times_eastern(subscription_id: str) -> Tuple[str, str, str]:
    """
    For past_due outreach: (current_period_end_eastern, latest_invoice_due_eastern,
    last_successful_payment_eastern). Empty strings if unknown.
    """
    period_end_e = ""
    inv_due_e = ""
    last_paid_e = ""

    try:
        sub = stripe.Subscription.retrieve(
            subscription_id,
            expand=["latest_invoice", "items.data"],
        )
        cpe = _subscription_period_end_unix(sub)
        if cpe is not None:
            period_end_e = _unix_to_eastern_str(cpe)

        li = getattr(sub, "latest_invoice", None)
        if li:
            if isinstance(li, str):
                li = stripe.Invoice.retrieve(li)
            inv_ts = _invoice_best_due_or_period_unix(li)
            if inv_ts is not None:
                inv_due_e = _unix_to_eastern_str(inv_ts)
    except stripe.StripeError:
        pass

    try:
        invs = stripe.Invoice.list(subscription=subscription_id, status="paid", limit=1)
        if invs.data:
            inv = invs.data[0]
            paid_at = _invoice_paid_at_unix(inv)
            if paid_at is not None:
                last_paid_e = _unix_to_eastern_str(paid_at)
            else:
                last_paid_e = _unix_to_eastern_str(getattr(inv, "created", None))
    except stripe.StripeError:
        pass

    return period_end_e, inv_due_e, last_paid_e


def _price_id(item: Any) -> str:
    p = item.price
    if isinstance(p, str):
        return p
    return p.id


def _iter_subscription_pages(
    *,
    status: str,
    starting_after: Optional[str] = None,
) -> Iterator[stripe.Subscription]:
    """Paginate subscriptions for a single status."""
    params: dict[str, Any] = {
        "status": status,
        "limit": 100,
        "expand": ["data.items.data.price"],
    }
    if starting_after:
        params["starting_after"] = starting_after

    while True:
        page = stripe.Subscription.list(**params)
        for sub in page.data:
            yield sub
        if not page.has_more or not page.data:
            break
        params["starting_after"] = page.data[-1].id


def iter_target_subscriptions(
    statuses: Sequence[str],
) -> Iterator[stripe.Subscription]:
    """All active then trialing (or whichever statuses requested), fully paginated."""
    for status in statuses:
        yield from _iter_subscription_pages(status=status)


def retrieve_subscriptions_by_ids(subscription_ids: Sequence[str]) -> List[stripe.Subscription]:
    out: List[stripe.Subscription] = []
    for sid in subscription_ids:
        sid = sid.strip()
        if not sid:
            continue
        sub = stripe.Subscription.retrieve(
            sid,
            expand=["items.data.price"],
        )
        out.append(sub)
    return out


def _recurring_interval(price: Any) -> Optional[str]:
    rec = getattr(price, "recurring", None)
    if rec is None:
        return None
    return getattr(rec, "interval", None)


def _recurring_usage_type(price: Any) -> Optional[str]:
    rec = getattr(price, "recurring", None)
    if rec is None:
        return None
    return getattr(rec, "usage_type", None)


def validate_price_pair(old_id: str, new_id: str) -> None:
    """Fail fast before any migration: new price usable; intervals and currency aligned."""
    if old_id == new_id:
        raise ValueError("OLD_PRICE_ID and NEW_PRICE_ID must be different.")

    old_price = stripe.Price.retrieve(old_id)
    new_price = stripe.Price.retrieve(new_id)

    if not new_price.active:
        raise ValueError(
            f"NEW_PRICE_ID {new_id} is inactive/archived in Stripe; refusing to migrate."
        )

    old_interval = _recurring_interval(old_price)
    new_interval = _recurring_interval(new_price)
    if not old_interval or not new_interval:
        raise ValueError("Both prices must be recurring (subscription) prices.")

    if old_interval != new_interval:
        raise ValueError(
            f"Recurring interval mismatch: old={old_interval!r} new={new_interval!r}"
        )

    old_currency = (getattr(old_price, "currency", None) or "").lower()
    new_currency = (getattr(new_price, "currency", None) or "").lower()
    if old_currency != new_currency:
        raise ValueError(f"Currency mismatch: old={old_currency!r} new={new_currency!r}")

    old_bs = getattr(old_price, "billing_scheme", None)
    new_bs = getattr(new_price, "billing_scheme", None)
    if old_bs != new_bs:
        raise ValueError(f"billing_scheme mismatch: old={old_bs!r} new={new_bs!r}")

    old_ut = _recurring_usage_type(old_price)
    new_ut = _recurring_usage_type(new_price)
    if old_ut != new_ut:
        raise ValueError(f"recurring.usage_type mismatch: old={old_ut!r} new={new_ut!r}")


@dataclass
class MigrationOutcome:
    subscription_id: str
    customer_id: str
    old_price_id: str
    new_price_id: str
    status: str
    result: str  # updated | would_update | skipped | failed
    detail: str


def classify_subscription(
    subscription: stripe.Subscription,
    old_price_id: str,
    new_price_id: str,
) -> Tuple[str, str, Optional[str]]:
    """
    Returns (decision, detail, subscription_item_id_to_update).
    decision is one of: update, skip
    """
    items = list(subscription.items.data)
    old_matches = [it for it in items if _price_id(it) == old_price_id]
    new_matches = [it for it in items if _price_id(it) == new_price_id]

    if not old_matches:
        return "skip", "no_subscription_item_with_old_price", None

    if new_matches:
        return "skip", "manual_review_old_and_new_price_both_present_on_items", None

    if len(old_matches) > 1:
        return "skip", "manual_review_multiple_items_with_old_price", None

    return "update", "", old_matches[0].id


def migrate_one(
    subscription: stripe.Subscription,
    old_price_id: str,
    new_price_id: str,
    dry_run: bool,
) -> MigrationOutcome:
    sub_id = subscription.id
    customer_id = subscription.customer
    if isinstance(customer_id, stripe.Customer):
        customer_id = customer_id.id
    status = subscription.status

    if status not in ("active", "trialing"):
        return MigrationOutcome(
            subscription_id=sub_id,
            customer_id=str(customer_id),
            old_price_id=old_price_id,
            new_price_id=new_price_id,
            status=status,
            result="skipped",
            detail=f"status_not_migrated:{status}",
        )

    decision, detail, item_id = classify_subscription(subscription, old_price_id, new_price_id)
    if decision == "skip":
        return MigrationOutcome(
            subscription_id=sub_id,
            customer_id=str(customer_id),
            old_price_id=old_price_id,
            new_price_id=new_price_id,
            status=status,
            result="skipped",
            detail=detail,
        )

    assert item_id is not None

    if dry_run:
        return MigrationOutcome(
            subscription_id=sub_id,
            customer_id=str(customer_id),
            old_price_id=old_price_id,
            new_price_id=new_price_id,
            status=status,
            result="would_update",
            detail="dry_run_no_api_write",
        )

    try:
        stripe.SubscriptionItem.modify(
            item_id,
            price=new_price_id,
            proration_behavior="none",
        )
    except stripe.StripeError as exc:
        return MigrationOutcome(
            subscription_id=sub_id,
            customer_id=str(customer_id),
            old_price_id=old_price_id,
            new_price_id=new_price_id,
            status=status,
            result="failed",
            detail=str(exc.user_message or exc),
        )

    return MigrationOutcome(
        subscription_id=sub_id,
        customer_id=str(customer_id),
        old_price_id=old_price_id,
        new_price_id=new_price_id,
        status=status,
        result="updated",
        detail="ok",
    )


def write_audit_row(writer: csv.DictWriter, outcome: MigrationOutcome, *, dry_run: bool) -> None:
    writer.writerow(
        {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "dry_run": str(dry_run),
            "subscription_id": outcome.subscription_id,
            "customer_id": outcome.customer_id,
            "old_price_id": outcome.old_price_id,
            "new_price_id": outcome.new_price_id,
            "status": outcome.status,
            "result": outcome.result,
            "detail": outcome.detail,
        }
    )


AUDIT_FIELDNAMES = [
    "timestamp_utc",
    "dry_run",
    "subscription_id",
    "customer_id",
    "old_price_id",
    "new_price_id",
    "status",
    "result",
    "detail",
]

# Stripe Subscription.list(status=...) values to scan in --diagnose (read-only reconciliation).
DIAGNOSE_STATUSES: Sequence[str] = (
    "active",
    "trialing",
    "past_due",
    "unpaid",
    "paused",
    "canceled",
    "incomplete",
    "incomplete_expired",
)

DIAGNOSE_FIELDNAMES = [
    "timestamp_utc",
    "subscription_id",
    "customer_id",
    "status",
    "cancel_at_period_end",
    "old_price_id",
    "old_price_item_count",
    "new_price_item_count",
    "migrator_eligible",
    "migrator_detail",
    "telegram_id",
    "telegram_username",
    "telegram_first_name",
    "telegram_lookup_source",
    "current_period_end_eastern",
    "latest_invoice_due_or_period_eastern",
    "last_successful_payment_eastern",
    "firestore_telegram_id",
    "firestore_tracked_stripe_subscription_id",
    "stripe_sub_matches_firestore",
]


def _try_firestore_client() -> Any:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        return None
    try:
        from google.cloud import firestore

        return firestore.Client(project=project)
    except Exception as exc:
        print(
            f"WARN: Firestore unavailable ({exc}); past_due Telegram lookup will use Stripe only.",
            flush=True,
        )
        return None


def _user_display_from_firestore(db: Any, telegram_id_str: str) -> Tuple[str, str]:
    """Return (username, first_name) from users/{telegram_id}, if present."""
    if not db or not telegram_id_str:
        return "", ""
    try:
        udoc = db.collection("users").document(telegram_id_str).get()
        if not udoc.exists:
            return "", ""
        ud = udoc.to_dict() or {}
        return str(ud.get("username") or ""), str(ud.get("first_name") or "")
    except Exception:
        return "", ""


def _stripe_metadata_get(metadata: Any, key: str) -> str:
    """
    Read Stripe metadata (dict or StripeObject). Do not call .get() on StripeObject — it is not a dict
    and will raise AttributeError.
    """
    if metadata is None:
        return ""
    if isinstance(metadata, dict):
        return str(metadata.get(key) or "").strip()
    try:
        return str(metadata[key] or "").strip()
    except (KeyError, TypeError):
        return ""


def _resolve_telegram_for_customer(customer_id: str, db: Any) -> Tuple[str, str, str, str]:
    """
    telegram_id, telegram_username, telegram_first_name, source.
    Order: Stripe customer metadata, then Firestore subscriptions + users (same as webhooks).
    """
    telegram_id = ""
    username = ""
    first_name = ""
    source = "not_found"

    try:
        cust = stripe.Customer.retrieve(customer_id)
        telegram_id = _stripe_metadata_get(cust.metadata, "telegram_id")
        if telegram_id:
            username = _stripe_metadata_get(cust.metadata, "telegram_username")
            source = "stripe_customer_metadata"
    except stripe.StripeError:
        return "", "", "", "stripe_error"

    if not telegram_id and db is not None:
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter

            q = (
                db.collection("subscriptions")
                .where(filter=FieldFilter("stripe_customer_id", "==", customer_id))
                .limit(1)
            )
            docs = list(q.stream())
            if docs:
                telegram_id = str(int(docs[0].id))
                source = "firestore_subscription"
        except Exception:
            pass

    if telegram_id and db is not None:
        u_user, u_first = _user_display_from_firestore(db, telegram_id)
        if not username and u_user:
            username = u_user
        if not first_name and u_first:
            first_name = u_first

    return telegram_id, username, first_name, source


def _firestore_subscription_for_customer_cached(
    db: Any,
    customer_id: str,
    cache: dict[str, Tuple[str, str]],
) -> Tuple[str, str]:
    """
    Firestore subscriptions doc for this Stripe customer: (telegram_id_str, stripe_subscription_id).
    One row per customer in typical AMBetz layout; cached per customer_id.
    """
    if customer_id in cache:
        return cache[customer_id]
    tid_out = ""
    sid_out = ""
    if db is not None:
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter

            q = (
                db.collection("subscriptions")
                .where(filter=FieldFilter("stripe_customer_id", "==", customer_id))
                .limit(1)
            )
            docs = list(q.stream())
            if docs:
                tid_out = str(int(docs[0].id))
                data = docs[0].to_dict() or {}
                sid_raw = data.get("stripe_subscription_id")
                if sid_raw:
                    sid_out = str(sid_raw).strip()
        except Exception:
            pass
    cache[customer_id] = (tid_out, sid_out)
    return tid_out, sid_out


def _firestore_match_label(this_stripe_sub_id: str, fs_tracked_sub_id: str, fs_telegram_id: str) -> str:
    if not fs_telegram_id and not fs_tracked_sub_id:
        return "no_firestore_doc"
    if not fs_tracked_sub_id:
        return "missing_firestore_stripe_subscription_id"
    return "yes" if this_stripe_sub_id == fs_tracked_sub_id else "no"


def _subscription_has_old_price(subscription: stripe.Subscription, old_price_id: str) -> bool:
    return any(_price_id(item) == old_price_id for item in subscription.items.data)


def _migrator_preview(
    subscription: stripe.Subscription,
    old_price_id: str,
    new_price_id: str,
) -> Tuple[str, str]:
    """
    Same rules as migrate_one (without API writes). Returns (eligible yes/no, detail).
    """
    status = subscription.status
    if status not in ("active", "trialing"):
        return "no", f"status_excluded:{status}"

    decision, detail, item_id = classify_subscription(subscription, old_price_id, new_price_id)
    if decision == "skip":
        return "no", detail
    assert item_id is not None
    return "yes", "would_call_subscription_item_modify"


def run_diagnose(
    *,
    old_price_id: str,
    new_price_id: str,
    csv_path: str,
) -> int:
    """List all subscriptions (every status) that include OLD_PRICE_ID on a line item."""
    written = 0
    by_status: dict[str, int] = {}
    eligible_yes = 0
    eligible_no = 0
    total_active_trialing_all_items = 0  # all active+trial subs, any price (reconciles vs migration loop)

    seen_old_price: dict[str, stripe.Subscription] = {}

    for status in DIAGNOSE_STATUSES:
        try:
            for sub in _iter_subscription_pages(status=status):
                if status in ("active", "trialing"):
                    total_active_trialing_all_items += 1
                if not _subscription_has_old_price(sub, old_price_id):
                    continue
                seen_old_price[sub.id] = sub
        except stripe.StripeError as exc:
            print(f"WARN: could not list status={status!r}: {exc}", flush=True)

    db = _try_firestore_client()
    timestamp = datetime.now(timezone.utc).isoformat()
    past_due_telegram_lines: List[str] = []
    firestore_customer_cache: dict[str, Tuple[str, str]] = {}
    past_due_firestore_mismatch = 0

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DIAGNOSE_FIELDNAMES)
        writer.writeheader()
        for sub in sorted(seen_old_price.values(), key=lambda s: s.id):
            st = sub.status
            by_status[st] = by_status.get(st, 0) + 1
            cust = sub.customer
            if isinstance(cust, stripe.Customer):
                cust = cust.id
            migrator_eligible, migrator_detail = _migrator_preview(sub, old_price_id, new_price_id)
            if migrator_eligible == "yes":
                eligible_yes += 1
            else:
                eligible_no += 1

            old_cnt = sum(1 for it in sub.items.data if _price_id(it) == old_price_id)
            new_cnt = sum(1 for it in sub.items.data if _price_id(it) == new_price_id)

            fs_tid, fs_sid, fs_match = "", "", ""
            if db is not None:
                fs_tid, fs_sid = _firestore_subscription_for_customer_cached(
                    db, str(cust), firestore_customer_cache
                )
                fs_match = _firestore_match_label(sub.id, fs_sid, fs_tid)

            tid, tun, tfn, tsrc = "", "", "", ""
            pe_e, inv_due_e, last_paid_e = "", "", ""
            if st == "past_due":
                tid, tun, tfn, tsrc = _resolve_telegram_for_customer(str(cust), db)
                pe_e, inv_due_e, last_paid_e = _past_due_billing_times_eastern(sub.id)
                if fs_match == "no":
                    past_due_firestore_mismatch += 1
                past_due_telegram_lines.append(
                    f"  sub={sub.id} customer={cust} telegram_id={tid or '(none)'} "
                    f"telegram_username={tun or '(none)'} telegram_first_name={tfn or '(none)'} "
                    f"source={tsrc} | current_period_end={pe_e or '(n/a)'} "
                    f"latest_invoice_due_or_period={inv_due_e or '(n/a)'} last_paid={last_paid_e or '(n/a)'} | "
                    f"firestore_tracked_sub={fs_sid or '(n/a)'} firestore_telegram_id={fs_tid or '(n/a)'} "
                    f"stripe_sub_matches_firestore={fs_match or '(no Firestore)'}"
                )

            writer.writerow(
                {
                    "timestamp_utc": timestamp,
                    "subscription_id": sub.id,
                    "customer_id": str(cust),
                    "status": st,
                    "cancel_at_period_end": str(bool(getattr(sub, "cancel_at_period_end", False))),
                    "old_price_id": old_price_id,
                    "old_price_item_count": str(old_cnt),
                    "new_price_item_count": str(new_cnt),
                    "migrator_eligible": migrator_eligible,
                    "migrator_detail": migrator_detail,
                    "telegram_id": tid,
                    "telegram_username": tun,
                    "telegram_first_name": tfn,
                    "telegram_lookup_source": tsrc if st == "past_due" else "",
                    "current_period_end_eastern": pe_e if st == "past_due" else "",
                    "latest_invoice_due_or_period_eastern": inv_due_e if st == "past_due" else "",
                    "last_successful_payment_eastern": last_paid_e if st == "past_due" else "",
                    "firestore_telegram_id": fs_tid,
                    "firestore_tracked_stripe_subscription_id": fs_sid,
                    "stripe_sub_matches_firestore": fs_match,
                }
            )
            written += 1
            f.flush()

    print("", flush=True)
    print("Diagnose summary (subscriptions with OLD_PRICE_ID on ≥1 line item)", flush=True)
    print(f"  total_found: {written}", flush=True)
    print("  by_status:", flush=True)
    for st in sorted(by_status.keys()):
        print(f"    {st}: {by_status[st]}", flush=True)
    print("", flush=True)
    n_active = by_status.get("active", 0)
    n_trialing = by_status.get("trialing", 0)
    n_past_due = by_status.get("past_due", 0)
    n_unpaid = by_status.get("unpaid", 0)
    print("Reconcile vs product page 'Active' count (same OLD_PRICE_ID, API truth):", flush=True)
    print(f"  subscriptions with status=active:   {n_active}", flush=True)
    print(f"  subscriptions with status=trialing: {n_trialing}", flush=True)
    print(f"  subscriptions with status=past_due: {n_past_due}", flush=True)
    print(f"  subscriptions with status=unpaid:   {n_unpaid}", flush=True)
    print(
        f"  active + past_due: {n_active + n_past_due}  "
        "(if Dashboard shows ~this number, it may be mixing strict 'active' with past_due)",
        flush=True,
    )
    print(
        "  In Dashboard: Billing → Subscriptions → filter Status 'Active' (expect "
        f"{n_active} on this price) and 'Past due' (expect {n_past_due} on this price).",
        flush=True,
    )
    print("", flush=True)
    print("Migrator would update (active/trialing + passes item checks):", flush=True)
    print(f"  eligible_yes: {eligible_yes}", flush=True)
    print(f"  eligible_no:  {eligible_no}", flush=True)
    print("", flush=True)
    print("Account totals (any price) — same as migration list scope:", flush=True)
    print(f"  active + trialing subscription count: {total_active_trialing_all_items}", flush=True)
    print(
        "  (Migration dry-run total_checked equals this, not 'subs on this price' only.)",
        flush=True,
    )
    print("", flush=True)
    print(f"Diagnose CSV: {csv_path}", flush=True)
    if past_due_telegram_lines:
        print("", flush=True)
        print(
            "Past due — Telegram + billing times (US Eastern; CSV columns on those rows):",
            flush=True,
        )
        for line in past_due_telegram_lines:
            print(line, flush=True)
        if past_due_firestore_mismatch:
            print("", flush=True)
            print(
                f"  Explainer: {past_due_firestore_mismatch} past_due line(s) have "
                "stripe_sub_matches_firestore=no — Stripe still has an older subscription on this price, "
                "while Firestore points at a different (usually newer) subscription id. "
                "The user can be paid up on the tracked sub; cancel the orphan in Stripe if appropriate.",
                flush=True,
            )
    print("", flush=True)
    print(
        "Outliers vs migration: rows with migrator_eligible=no and status not active/trialing, "
        "or migrator_detail not status_excluded (shape skips).",
        flush=True,
    )
    return 0


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate subscription items from OLD_PRICE_ID to NEW_PRICE_ID (in place, no proration)."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Perform real Stripe updates. Without this flag, dry-run only.",
    )
    parser.add_argument(
        "--max-subscriptions",
        type=int,
        default=None,
        metavar="N",
        help="Stop after processing N subscriptions (listing/retrieve order). For testing.",
    )
    parser.add_argument(
        "--subscription-ids",
        type=str,
        default=None,
        help="Comma-separated subscription IDs to process only (still applies status/validation rules).",
    )
    parser.add_argument(
        "--audit-csv",
        type=str,
        default=None,
        help="Path for CSV audit log. Default: stripe_price_migration_audit_<utc>.csv in cwd.",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Read-only: list every subscription (all statuses) that has OLD_PRICE_ID on a line item; "
        "write CSV and show migrator eligibility. For past_due rows: Telegram id/username "
        "(Stripe metadata, then Firestore if GOOGLE_CLOUD_PROJECT is set), plus period end / "
        "invoice due / last paid time in US Eastern. No subscription updates.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_dotenv()

    args = parse_args(argv)

    secret_key = os.environ.get("STRIPE_SECRET_KEY")
    old_price_id = os.environ.get("OLD_PRICE_ID")
    new_price_id = os.environ.get("NEW_PRICE_ID")

    if not secret_key:
        print("ERROR: STRIPE_SECRET_KEY is not set.", file=sys.stderr)
        return 1
    if not old_price_id or not new_price_id:
        print("ERROR: OLD_PRICE_ID and NEW_PRICE_ID must be set.", file=sys.stderr)
        return 1

    stripe.api_key = secret_key

    if args.diagnose:
        diagnose_csv = args.audit_csv
        if not diagnose_csv:
            diagnose_csv = datetime.now(timezone.utc).strftime(
                "stripe_diagnose_old_price_%Y%m%dT%H%M%SZ.csv"
            )
        try:
            stripe.Price.retrieve(old_price_id)
        except stripe.StripeError as exc:
            print(f"ERROR: OLD_PRICE_ID not retrievable: {exc}", file=sys.stderr)
            return 1
        try:
            stripe.Price.retrieve(new_price_id)
        except stripe.StripeError as exc:
            print(f"ERROR: NEW_PRICE_ID not retrievable: {exc}", file=sys.stderr)
            return 1
        print("Mode: DIAGNOSE (read-only, no writes)", flush=True)
        return run_diagnose(
            old_price_id=old_price_id,
            new_price_id=new_price_id,
            csv_path=diagnose_csv,
        )

    dry_run = not args.live

    try:
        validate_price_pair(old_price_id, new_price_id)
    except (stripe.StripeError, ValueError) as exc:
        print(f"ERROR: Price validation failed: {exc}", file=sys.stderr)
        return 1

    audit_path = args.audit_csv
    if not audit_path:
        audit_path = datetime.now(timezone.utc).strftime("stripe_price_migration_audit_%Y%m%dT%H%M%SZ.csv")

    id_filter: Optional[List[str]] = None
    if args.subscription_ids:
        id_filter = [s.strip() for s in args.subscription_ids.split(",") if s.strip()]

    total_checked = 0
    total_updated = 0
    total_would_update = 0
    total_skipped = 0
    total_failed = 0

    print(
        f"Mode: {'DRY-RUN (no API writes)' if dry_run else 'LIVE (will modify matching subscription items only)'}",
        flush=True,
    )
    print(f"Audit CSV: {audit_path}", flush=True)

    with open(audit_path, "w", newline="", encoding="utf-8") as audit_file:
        writer = csv.DictWriter(audit_file, fieldnames=AUDIT_FIELDNAMES)
        writer.writeheader()
        audit_file.flush()

        if id_filter:
            subscriptions = retrieve_subscriptions_by_ids(id_filter)
        else:
            subscriptions = iter_target_subscriptions(["active", "trialing"])

        for subscription in subscriptions:
            if args.max_subscriptions is not None and total_checked >= args.max_subscriptions:
                break

            total_checked += 1
            outcome = migrate_one(subscription, old_price_id, new_price_id, dry_run=dry_run)

            # Dry-run "would update" counts as skipped for summary, but we expose clearly in CSV
            if outcome.result == "updated":
                total_updated += 1
            elif outcome.result == "would_update":
                total_would_update += 1
            elif outcome.result == "failed":
                total_failed += 1
            else:
                total_skipped += 1

            write_audit_row(writer, outcome, dry_run=dry_run)
            audit_file.flush()

            line = (
                f"sub={outcome.subscription_id} customer={outcome.customer_id} "
                f"status={outcome.status} result={outcome.result} detail={outcome.detail}"
            )
            print(line, flush=True)

    print("", flush=True)
    print("Summary", flush=True)
    print(f"  total_checked:     {total_checked}", flush=True)
    if dry_run:
        print(f"  total_would_update: {total_would_update}", flush=True)
    print(f"  total_updated:      {total_updated}", flush=True)
    print(f"  total_skipped:      {total_skipped}", flush=True)
    print(f"  total_failed:       {total_failed}", flush=True)

    if dry_run:
        print("", flush=True)
        print("Dry-run complete. Re-run with --live after reviewing the audit CSV.", flush=True)

    return 0 if total_failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
