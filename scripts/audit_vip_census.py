#!/usr/bin/env python3
"""
Census: how many people have a current (Firestore) paid subscription, and of those
who do *not* (lapsed in Firestore), how many are still `member` in *both* VIP groups.

Active definition (mirrors /rejoin-style checks, Firestore only):
  status == "active" AND expiry_date > now (UTC)
  (Stripe is still source of truth in production; this is a Firestore mirror snapshot.)

"Both chats" uses VIP_ANNOUNCEMENTS_ID and VIP_CHAT_ID. If you only set one (or both equal),
that single id is used for the "in both" check (one membership = in both, same as one group).
Only checks telegram_ids that appear in Firestore `subscriptions/`. People in the groups
with no `subscriptions` doc are not included (Bot API has no list-all-members for large groups).

Usage (repo root):
  python3 scripts/audit_vip_census.py

Requires: GOOGLE_CLOUD_PROJECT, ADC, TELEGRAM_BOT_TOKEN, VIP_* chat ids, STRIPE not required.
Read-only. May take a while: one getChatMember per lapsed user per chat (2 x N calls).
Optional: --max-lapsed-checks 500  to cap Telegram calls for dry safety.
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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pytz
from dotenv import load_dotenv

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))


def _bot_token() -> Optional[str]:
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN_TEST"):
        t = (os.environ.get(key) or "").strip()
        if t:
            return t
    return None


def _vip_chat_ids() -> List[Tuple[str, int]]:
    out: List[Tuple[str, int]] = []
    seen: Set[int] = set()
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
            print(f"Warning: {ev} is not an int, skipping", file=sys.stderr)
            continue
        if cid in seen:
            continue
        seen.add(cid)
        out.append((label, cid))
    return out


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


def _in_group_status(st: str) -> bool:
    return st in ("member", "administrator", "creator", "restricted")


def _norm_expiry(exp: Any) -> Optional[datetime]:
    if exp is None:
        return None
    if hasattr(exp, "tzinfo") and exp.tzinfo is None:
        return pytz.UTC.localize(exp)
    return exp


def _entitled_in_firestore(sub: Dict[str, Any], now: datetime) -> bool:
    if sub.get("status") != "active":
        return False
    exp = _norm_expiry(sub.get("expiry_date"))
    if not exp:
        return False
    return exp > now


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--max-lapsed-checks",
        type=int,
        default=0,
        metavar="N",
        help="Cap how many lapsed users get Telegram checks (0 = no cap).",
    )
    ap.add_argument(
        "--delay-seconds",
        type=float,
        default=0.05,
        help="Pause between each user's Telegram round-trip (default 0.05).",
    )
    args = ap.parse_args()

    load_dotenv(_REPO / ".env")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        print("Set GOOGLE_CLOUD_PROJECT.", file=sys.stderr)
        sys.exit(1)
    token = _bot_token()
    chats = _vip_chat_ids()
    if not token or not chats:
        print(
            "Need TELEGRAM_BOT_TOKEN and at least one of VIP_ANNOUNCEMENTS_ID / VIP_CHAT_ID.",
            file=sys.stderr,
        )
        sys.exit(1)
    # Two distinct groups, or one id used for both (deduped in _vip_chat_ids): treat as same
    if len(chats) >= 2:
        ann = chats[0][1]
        disc = chats[1][1]
    else:
        ann = disc = chats[0][1]

    from firestore_service import FirestoreService

    fs = FirestoreService(project_id=project)
    now = datetime.now(pytz.UTC)

    active_count = 0
    lapsed: List[Tuple[int, Dict]] = []
    for doc in fs.db.collection("subscriptions").stream():
        d = doc.to_dict() or {}
        tid = int(doc.id)
        d["_id"] = tid
        if _entitled_in_firestore(d, now):
            active_count += 1
        else:
            lapsed.append((tid, d))

    print("=== Firestore `subscriptions` snapshot (UTC) ===\n", flush=True)
    print(f"Subscribed in Firestore (active + not past expiry): {active_count}\n", flush=True)

    print(
        f"Lapsed in Firestore (any other case: expired, or active but past expiry, etc.): {len(lapsed)}",
        flush=True,
    )
    if not lapsed:
        print("No lapsed to check in Telegram.\n", flush=True)
        return

    to_check = lapsed
    if args.max_lapsed_checks and len(to_check) > args.max_lapsed_checks:
        print(
            f"(Capping Telegram checks to first {args.max_lapsed_checks} of {len(lapsed)}.)\n",
            flush=True,
        )
        to_check = lapsed[: args.max_lapsed_checks]

    both_in: List[int] = []
    ann_only: List[int] = []
    disc_only: List[int] = []
    neither: List[int] = []
    t_errors = 0
    same_chat = ann == disc

    for i, (tid, _doc) in enumerate(to_check):
        st_a = _get_chat_member_status(token, ann, tid)
        time.sleep(args.delay_seconds)
        if same_chat:
            st_d = st_a
        else:
            st_d = _get_chat_member_status(token, disc, tid)
            time.sleep(args.delay_seconds)

        ok_a = st_a.startswith("error:")
        ok_d = st_d.startswith("error:")
        if ok_a or (not same_chat and ok_d) or (same_chat and ok_a):
            t_errors += 1
        a_in = _in_group_status(st_a) if not ok_a else False
        d_in = _in_group_status(st_d) if not ok_d else False

        if a_in and d_in:
            both_in.append(tid)
        elif a_in and not d_in and not same_chat:
            ann_only.append(tid)
        elif d_in and not a_in and not same_chat:
            disc_only.append(tid)
        else:
            neither.append(tid)

    print(
        f"\n=== Lapsed in Firestore: Telegram membership "
        f"(checked {len(to_check)} of {len(lapsed)} lapsed) ===\n",
        flush=True,
    )
    if t_errors:
        print(
            f"Note: {t_errors} user(s) had at least one getChatMember error (not counted as in group).\n",
            flush=True,
        )
    if same_chat:
        print(
            f"Lapsed but still in the VIP group (one chat_id configured for both): {len(both_in)}",
            flush=True,
        )
    else:
        print(
            f"Still in *both* VIP chats (lapsed in Firestore): {len(both_in)}",
            flush=True,
        )
    if both_in:
        print(f"  telegram_ids: {both_in}\n", flush=True)
    if not same_chat:
        print(f"In announcements only: {len(ann_only)}  -> {ann_only or '—'}", flush=True)
        print(f"In discussion only:   {len(disc_only)}  -> {disc_only or '—'}", flush=True)
    print(f"In neither:           {len(neither)}", flush=True)
    if args.max_lapsed_checks and len(lapsed) > args.max_lapsed_checks:
        print(
            f"\n(There are {len(lapsed) - args.max_lapsed_checks} more lapsed users not checked; "
            f"increase --max-lapsed-checks or remove cap.)",
            flush=True,
        )


if __name__ == "__main__":
    main()
