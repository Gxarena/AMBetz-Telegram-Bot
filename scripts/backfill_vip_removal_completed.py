#!/usr/bin/env python3
"""
Set `vip_removal_completed_at` on `subscriptions/{telegram_id}` for specific users
so the stale branch of `find_expired_subscriptions()` stops re-queuing them (same field
as `mark_vip_removal_completed` in production).

Use after deploying the idempotency fix, or to stop repeat admin notifications for
users who were already kicked but never got the field before deploy.

Prereqs (repo root):
  - GOOGLE_CLOUD_PROJECT (or in .env)
  - Application Default Credentials with Firestore access

Usage:
  python3 scripts/backfill_vip_removal_completed.py --dry-run --ids 6972506628,8553646080
  python3 scripts/backfill_vip_removal_completed.py --ids 6972506628,5417234087,...

  python3 scripts/backfill_vip_removal_completed.py --file ids.txt

Example IDs from a 2026-04-23 removal log (verify before running):
  6972506628,8553646080,1679019590,5814570996,5966731944,6778919240,5417234087,8046382574,
  6800986350,5530948550,8147305912,5248271099,7101151769,6657157602
  (If you also have 6800966350 for @Ecco_Eats, add it; screenshots differ by one digit.)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from firestore_service import FirestoreService  # noqa: E402


def _parse_ids(arg: str) -> list[int]:
    out: list[int] = []
    for part in arg.replace("\n", ",").split(","):
        s = part.strip()
        if not s:
            continue
        out.append(int(s))
    return out


def _load_ids_file(path: Path) -> list[int]:
    out: list[int] = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        out.append(int(line))
    return out


def main() -> int:
    load_dotenv(_REPO / ".env")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--ids",
        help="Comma-separated Telegram user ids (subscriptions document ids).",
    )
    ap.add_argument(
        "--file",
        type=Path,
        help="Text file: one id per line (inline # comments allowed).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions only, do not write to Firestore.",
    )
    args = ap.parse_args()

    ids: list[int] = []
    if args.file:
        ids.extend(_load_ids_file(args.file))
    if args.ids:
        ids.extend(_parse_ids(args.ids))
    if not ids:
        ap.error("Provide --ids and/or --file")

    seen: set[int] = set()
    unique = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            unique.append(i)

    if not os.getenv("GOOGLE_CLOUD_PROJECT"):
        print("GOOGLE_CLOUD_PROJECT is not set", file=sys.stderr)
        return 1

    fs = FirestoreService()
    ok_n = 0
    for tid in unique:
        sub = fs.get_subscription(tid)
        if not sub:
            print(f"skip  {tid}  (no subscriptions/ document)")
            continue
        if args.dry_run:
            print(f"dry-run: would set vip_removal_completed_at on {tid}")
            ok_n += 1
            continue
        if fs.mark_vip_removal_completed(tid):
            print(f"ok    {tid}")
            ok_n += 1
        else:
            print(f"fail  {tid}", file=sys.stderr)

    print(f"Done. Updated {ok_n} of {len(unique)} id(s) (skipped if no sub doc).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
