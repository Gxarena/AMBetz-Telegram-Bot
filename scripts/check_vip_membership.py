#!/usr/bin/env python3
"""
Read-only: for each Telegram user id, call getChatMember in each configured VIP
supergroup and print status (member = still in, left/kicked = not in).

If you omit --telegram-ids, uses the same id set as find_expired_subscriptions() in Firestore.
Pass --telegram-ids to check a custom list (e.g. the 14 user ids you kicked).

Requires in .env:
  TELEGRAM_BOT_TOKEN (or TELEGRAM_BOT_TOKEN_TEST)
  VIP_ANNOUNCEMENTS_ID and/or VIP_CHAT_ID (numeric, negative for supergroups)

If you do not pass --telegram-ids: also GOOGLE_CLOUD_PROJECT + Firestore ADC.

Usage (repo root):
  python3 scripts/check_vip_membership.py
  python3 scripts/check_vip_membership.py --telegram-ids 6972506628,8553646080,...
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
            return f"error:{err.get('description', body)[:100]}"
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--telegram-ids",
        type=str,
        default="",
        help="Comma-separated telegram user ids. If omitted, uses find_expired_subscriptions() from Firestore.",
    )
    args = ap.parse_args()

    load_dotenv(_REPO / ".env")
    token = _bot_token()
    chats = _vip_chat_ids()
    if not token:
        print("Set TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN_TEST.", file=sys.stderr)
        sys.exit(1)
    if not chats:
        print("Set VIP_ANNOUNCEMENTS_ID and/or VIP_CHAT_ID to numeric chat ids.", file=sys.stderr)
        sys.exit(1)

    ids: List[int] = []
    if (args.telegram_ids or "").strip():
        for part in args.telegram_ids.split(","):
            part = part.strip()
            if not part:
                continue
            ids.append(int(part))
    else:
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not project:
            print("Set GOOGLE_CLOUD_PROJECT (or pass --telegram-ids).", file=sys.stderr)
            sys.exit(1)
        from firestore_service import FirestoreService

        fs = FirestoreService(project_id=project)
        rows = fs.find_expired_subscriptions()
        ids = [int(s["telegram_id"]) for s in rows]

    if not ids:
        print("No telegram ids to check (empty list).")
        return

    any_still: List[int] = []
    every_call_failed = True
    for tid in sorted(ids):
        parts: List[str] = []
        still_here = False
        statuses: List[str] = []
        for label, cid in chats:
            st = _get_chat_member_status(token, cid, tid)
            statuses.append(st)
            ing = _in_group_status(st)
            if ing:
                still_here = True
            parts.append(f"{label}={st}")
        if not all(s.startswith("error:") for s in statuses):
            every_call_failed = False
        if still_here:
            any_still.append(tid)
        line = f"telegram_id={tid}\t" + "  ".join(parts) + f"\t{'STILL_IN_GROUP' if still_here else 'out_or_error'}"
        print(line)

    print("---", flush=True)
    if every_call_failed and ids:
        print(
            "WARNING: Every call returned an error (often bad VIP_ANNOUNCEMENTS_ID / VIP_CHAT_ID in .env, "
            "or bot not in the group). Fix .env to match production Secret Manager ids, then re-run.",
            file=sys.stderr,
        )
        sys.exit(2)
    if any_still:
        print(
            f"Summary: {len(any_still)} user(s) still appear in at least one VIP chat: {any_still}",
            flush=True,
        )
        sys.exit(1)
    print(
        f"Summary: none of the {len(ids)} user(s) are member/admin/creator/restricted in the configured chats.",
        flush=True,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
