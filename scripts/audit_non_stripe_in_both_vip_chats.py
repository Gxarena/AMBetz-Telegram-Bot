#!/usr/bin/env python3
"""
Find Telegram users who are in *both* VIP supergroups (per .env chat ids) but whose
Telegram id is **not** in the live Stripe set: active/trialing subscription with a
future billing period end. The Stripe set is built once from the API (like your ~31
live subs), not from Firestore customer ids.

**Telegram Bot API cannot list all members of a chat** (only getChatMember per user).
So you supply member telegram ids yourself — e.g. export from Telegram Desktop + script,
Pyrogram/Telethon, or admin tooling — one id per line:

  --intersect-files announcements.txt discussion.txt
      Each file = all member ids in that chat. Candidates = intersection (~≤ min(67,62)).

  --candidates-file in_both_chats.txt
      Ids you already know are in both chats; each is still checked with getChatMember.

  --firestore-candidates
      Legacy: union of Firestore `users/*` and `subscriptions/*` (often 400+; slow).

Usage (repo root):
  python3 scripts/audit_non_stripe_in_both_vip_chats.py \\
      --intersect-files vip_ann_members.txt vip_disc_members.txt
  python3 scripts/audit_non_stripe_in_both_vip_chats.py --candidates-file both.txt
  python3 scripts/audit_non_stripe_in_both_vip_chats.py --firestore-candidates

Progress prints every 50 ids by default (--progress-every 0 to disable).

Requires: GOOGLE_CLOUD_PROJECT, ADC, STRIPE_SECRET_KEY, TELEGRAM_BOT_TOKEN,
          VIP_ANNOUNCEMENTS_ID and/or VIP_CHAT_ID (two distinct ids, or one shared id).
Read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import stripe
from dotenv import load_dotenv

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from firestore_service import FirestoreService  # noqa: E402
from gcp_stripe_service import _subscription_period_bounds_unix  # noqa: E402
from stripe_compat import metadata_get  # noqa: E402


def _bot_token() -> Optional[str]:
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN_TEST"):
        t = (os.environ.get(key) or "").strip()
        if t:
            return t
    return None


def _vip_ann_disc() -> Tuple[int, int, bool]:
    """(announcements_chat_id, discussion_chat_id, same_chat)."""
    seen: Set[int] = set()
    slots: List[Tuple[str, int]] = []
    for label, ev in (
        ("announcements", "VIP_ANNOUNCEMENTS_ID"),
        ("discussion", "VIP_CHAT_ID"),
    ):
        raw = (os.environ.get(ev) or "").strip()
        if not raw:
            continue
        try:
            cid = int(raw)
        except ValueError:
            continue
        if cid in seen:
            continue
        seen.add(cid)
        slots.append((label, cid))
    if not slots:
        raise SystemExit("Set VIP_ANNOUNCEMENTS_ID and/or VIP_CHAT_ID.")
    if len(slots) >= 2:
        return slots[0][1], slots[1][1], False
    return slots[0][1], slots[0][1], True


def _get_chat_member_status(token: str, chat_id: int, user_id: int) -> str:
    base = f"https://api.telegram.org/bot{token}/getChatMember"
    q = urllib.parse.urlencode({"chat_id": chat_id, "user_id": user_id})
    url = f"{base}?{q}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data: Dict[str, Any] = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()
            err = json.loads(body)
            return f"error:{err.get('description', body)[:120]}"
        except Exception:
            return f"error:HTTP {e.code}"
    except Exception as e:
        return f"error:{e}"
    if not data.get("ok"):
        return f"error:{data.get('description', 'not ok')}"
    st = (data.get("result") or {}).get("status")
    return str(st) if st is not None else "error:no status"


def _in_group(st: str) -> bool:
    return st in ("member", "administrator", "creator", "restricted")


def _list_subscriptions_paginated(status: str) -> List[Any]:
    """Same shape as audit_stripe_active_vs_firestore (expanded customer)."""
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


def _telegram_id_from_subscription(sub: Any) -> Optional[int]:
    """Prefer customer.metadata.telegram_id; fallback subscription.metadata."""
    cust = getattr(sub, "customer", None) if not hasattr(sub, "get") else sub.get("customer")
    if isinstance(cust, str):
        try:
            cust = stripe.Customer.retrieve(cust)
        except stripe.error.StripeError:
            cust = None
    if cust is not None:
        raw = metadata_get(getattr(cust, "metadata", None), "telegram_id")
        if raw is not None and str(raw).strip().isdigit():
            return int(str(raw).strip())
    raw = metadata_get(getattr(sub, "metadata", None), "telegram_id")
    if raw is not None and str(raw).strip().isdigit():
        return int(str(raw).strip())
    return None


def _live_entitled_telegram_ids() -> Set[int]:
    """Telegram ids that have at least one active/trialing sub with future period end (live key)."""
    now = int(time.time())
    entitled: Set[int] = set()
    for status in ("active", "trialing"):
        for sub in _list_subscriptions_paginated(status):
            _, pe = _subscription_period_bounds_unix(sub)
            if pe is None or pe <= now:
                continue
            tid = _telegram_id_from_subscription(sub)
            if tid is not None:
                entitled.add(tid)
    return entitled


def _user_label(fs: FirestoreService, tid: int) -> str:
    u = fs.get_user(tid)
    if not u:
        return "(no users/ doc)"
    parts: List[str] = []
    if u.get("username"):
        parts.append(f"@{u['username']}")
    fn = (u.get("first_name") or "").strip()
    ln = (u.get("last_name") or "").strip()
    if fn or ln:
        parts.append(" ".join(p for p in (fn, ln) if p))
    return " | ".join(parts) if parts else "(empty user fields)"


def _collect_candidate_ids(fs: FirestoreService) -> List[int]:
    ids: Set[int] = set()
    for doc in fs.db.collection("users").stream():
        try:
            ids.add(int(doc.id))
        except ValueError:
            pass
    for doc in fs.db.collection("subscriptions").stream():
        try:
            ids.add(int(doc.id))
        except ValueError:
            pass
    return sorted(ids)


def _read_id_file(path: Path) -> Set[int]:
    """One telegram user id per line; # comments and blank lines allowed."""
    text = path.read_text(encoding="utf-8", errors="replace")
    out: Set[int] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        token = line.split()[0]
        try:
            out.add(int(token))
        except ValueError:
            raise SystemExit(f"Non-numeric telegram id in {path}: {line!r}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--intersect-files",
        nargs=2,
        metavar=("ANN_TXT", "DISC_TXT"),
        help="Member ids per chat (one int per line); candidates = sorted intersection.",
    )
    src.add_argument(
        "--candidates-file",
        metavar="PATH",
        help="Ids already in both chats (one per line); verified via getChatMember.",
    )
    src.add_argument(
        "--firestore-candidates",
        action="store_true",
        help="Candidates = Firestore users ∪ subscriptions (large; legacy).",
    )
    ap.add_argument("--delay-seconds", type=float, default=0.05, help="Between Telegram calls.")
    ap.add_argument(
        "--progress-every",
        type=int,
        default=50,
        metavar="N",
        help="Print progress every N candidates (0 = off). Default 50.",
    )
    args = ap.parse_args()

    load_dotenv(_REPO / ".env")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    sk = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
    if not project or not sk:
        print("Set GOOGLE_CLOUD_PROJECT and STRIPE_SECRET_KEY.", file=sys.stderr)
        sys.exit(1)
    token = _bot_token()
    if not token:
        print("Set TELEGRAM_BOT_TOKEN.", file=sys.stderr)
        sys.exit(1)
    stripe.api_key = sk

    ann, disc, same_chat = _vip_ann_disc()
    fs = FirestoreService(project_id=project)

    if args.intersect_files:
        p_ann, p_disc = (Path(p).expanduser() for p in args.intersect_files)
        s_ann, s_disc = _read_id_file(p_ann), _read_id_file(p_disc)
        candidates = sorted(s_ann & s_disc)
        cand_desc = (
            f"intersection of {p_ann} ({len(s_ann)} ids) ∩ {p_disc} ({len(s_disc)} ids)"
        )
    elif args.candidates_file:
        p = Path(args.candidates_file).expanduser()
        candidates = sorted(_read_id_file(p))
        cand_desc = f"candidates file {p} ({len(candidates)} ids)"
    else:
        candidates = _collect_candidate_ids(fs)
        cand_desc = "Firestore users ∪ subscriptions"

    entitled_ids = _live_entitled_telegram_ids()
    print(
        f"Live Stripe entitled telegram ids (active/trialing, future period_end): {len(entitled_ids)}",
        flush=True,
    )
    print(
        f"Scanning {len(candidates)} candidate telegram ids ({cand_desc}). "
        f"VIP ann={ann} disc={disc} same_chat={same_chat}\n",
        flush=True,
    )

    in_both_not_stripe: List[Tuple[int, str, str]] = []
    tg_errors = 0
    in_both_stripe_ok = 0
    skipped_not_in_both = 0
    n = len(candidates)

    for i, tid in enumerate(candidates, start=1):
        if args.progress_every and i % args.progress_every == 0:
            print(f"  progress {i}/{n} …", flush=True)

        st_a = _get_chat_member_status(token, ann, tid)
        time.sleep(args.delay_seconds)
        if st_a.startswith("error:"):
            tg_errors += 1
            continue
        if same_chat:
            st_d = st_a
        else:
            # Not in announcements → cannot be "in both"; skip 2nd Telegram call
            if not _in_group(st_a):
                continue
            st_d = _get_chat_member_status(token, disc, tid)
            time.sleep(args.delay_seconds)

        if st_d.startswith("error:"):
            tg_errors += 1
            continue
        if not (_in_group(st_a) and _in_group(st_d)):
            skipped_not_in_both += 1
            continue

        if tid in entitled_ids:
            in_both_stripe_ok += 1
        else:
            in_both_not_stripe.append(
                (tid, _user_label(fs, tid), "not in live active/trialing future-period set"),
            )

    print("=== In BOTH VIP chats but NOT in live entitled Telegram id set ===\n", flush=True)
    if not in_both_not_stripe:
        print("(none among candidates)\n", flush=True)
    else:
        for tid, who, why in in_both_not_stripe:
            print(f"telegram_id={tid}\t{who}\t{why}", flush=True)
        print(f"\nCount: {len(in_both_not_stripe)}", flush=True)

    print("\n---", flush=True)
    print(f"In both chats WITH Stripe entitlement: {in_both_stripe_ok}", flush=True)
    print(f"getChatMember errors (skipped): {tg_errors}", flush=True)
    print(
        f"Candidates not in both chats at verify time (skipped): {skipped_not_in_both}",
        flush=True,
    )
    if args.firestore_candidates:
        print(
            "\nNote: --firestore-candidates can miss group-only users with no Firestore doc.",
            flush=True,
        )


if __name__ == "__main__":
    main()
