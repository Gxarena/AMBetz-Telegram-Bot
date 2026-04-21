#!/usr/bin/env python3
"""
Read-only preview: who `find_expired_subscriptions()` would process on the next
`POST /check-expired` (or the bot’s scheduled `check_expired_subscriptions`).

Same Firestore query as production, including:
- status=active, expiry in the past (with recurring 5-min grace in code), and
- status=expired, expiry in the past, within the recent lookback (see `firestore_service`).

This script only reads Firestore (and optionally Telegram for membership). It does not kick.
The real `POST /check-expired` also calls Stripe *before* kicking; this preview does not, so
some rows may be skipped in production if Stripe still shows an active sub (Firestore resynced).

Usage (repo root, with GOOGLE_CLOUD_PROJECT and ADC for Firestore):
  python3 scripts/list_expired_pending_kick_firestore.py
  python3 scripts/list_expired_pending_kick_firestore.py --check-telegram

`--check-telegram` needs TELEGRAM_BOT_TOKEN (or TELEGRAM_BOT_TOKEN_TEST) and one or more of
VIP_ANNOUNCEMENTS_ID, VIP_CHAT_ID (see .env.example). Uses getChatMember (read-only).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from firestore_service import FirestoreService  # noqa: E402


def _user_label(fs: FirestoreService, telegram_id: int) -> str:
    """Display name from Firestore users/{id} (what the bot last stored)."""
    u = fs.get_user(telegram_id)
    if not u:
        return "name=(no users/ document)"
    parts: list[str] = []
    un = u.get("username")
    if un:
        parts.append(f"@{un}")
    fn = (u.get("first_name") or "").strip()
    ln = (u.get("last_name") or "").strip()
    if fn or ln:
        parts.append(" ".join(p for p in (fn, ln) if p).strip())
    if not parts:
        return "name=(empty user fields)"
    return " | ".join(parts)


def _bot_token() -> Optional[str]:
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN_TEST"):
        t = (os.environ.get(key) or "").strip()
        if t:
            return t
    return None


def _vip_chat_ids() -> List[Tuple[str, int]]:
    """(label, chat_id) for distinct configured VIP group ids."""
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
    """Telegram member status string, or an error/unknown token."""
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
            return f"error:{err.get('description', body)[:80]}"
        except Exception:
            return f"error:HTTP {e.code}"
    except Exception as e:
        return f"error:{e}"
    if not data.get("ok"):
        return f"error:{data.get('description', 'not ok')}"
    st = (data.get("result") or {}).get("status")
    return str(st) if st is not None else "error:no status"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--check-telegram",
        action="store_true",
        help="For each id, call getChatMember for each configured VIP chat (read-only).",
    )
    args = p.parse_args()

    load_dotenv(_REPO / ".env")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        print("Set GOOGLE_CLOUD_PROJECT (and authenticate for Firestore).", file=sys.stderr)
        sys.exit(1)
    token = _bot_token() if args.check_telegram else None
    chat_slots = _vip_chat_ids() if args.check_telegram else []
    if args.check_telegram:
        if not token:
            print(
                "Set TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN_TEST in .env for --check-telegram.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not chat_slots:
            print(
                "Set VIP_ANNOUNCEMENTS_ID and/or VIP_CHAT_ID in .env (numeric Telegram chat id).",
                file=sys.stderr,
            )
            sys.exit(1)

    fs = FirestoreService(project_id=project)
    rows = fs.find_expired_subscriptions()
    if not rows:
        print("No one would be processed: empty find_expired_subscriptions() set.")
        return
    print(
        f"Count: {len(rows)} — would be processed on next check (Stripe refresh + kick if needed)\n"
    )
    for s in sorted(rows, key=lambda x: (x.get("expiry_date") or "")):
        tid = int(s.get("telegram_id", 0))
        exp = s.get("expiry_date")
        st = s.get("status")
        reason = s.get("expire_reason") or "-"
        sid = s.get("stripe_subscription_id") or "-"
        stype = s.get("subscription_type") or "-"
        who = _user_label(fs, tid)
        line = (
            f"telegram_id={tid}\t{who}\t"
            f"status={st}\texpire_reason={reason}\texpiry_date={exp}\t"
            f"subscription_type={stype}\tstripe_subscription_id={sid}"
        )
        if args.check_telegram and token and chat_slots:
            mem_parts = []
            for label, cid in chat_slots:
                mst = _get_chat_member_status(token, cid, tid)
                # member, administrator, creator, restricted = still in group
                in_g = mst in (
                    "member",
                    "administrator",
                    "creator",
                    "restricted",
                )
                mem_parts.append(f"{label}:{mst}({'in' if in_g else 'out'})")
            line = line + "\t" + " ".join(mem_parts)
        print(line)


if __name__ == "__main__":
    main()
