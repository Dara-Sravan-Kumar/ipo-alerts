"""
dryrun.py
=========
Fetch live IPO facts (Groww -> IPOAlerts -> NSE+BSE) + GMP (aggregator), merge
them, and PRINT the result to the terminal. No AI calls, no Discord, no state
writes.

Use it to validate that the live data sources and the GMP merge actually work
before spending AI tokens or posting anything:

    python main.py --dry-run

Only needs (optionally) the GMP aggregator keys; it does NOT require AI_API_KEY
or any Discord credentials, so it bypasses the full config validation.
"""

from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv

from fetcher import AllProvidersFailed, IPOFetcher
from gmp import GMPResolver

log = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError:
        return default


def run_dry_run() -> int:
    load_dotenv()

    # Make sure ₹ and other non-ASCII print cleanly on the Windows console.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - older interpreters / redirected streams
        pass

    lookahead = _int_env("LOOKAHEAD_DAYS", 21)

    print("=" * 70)
    print(f"DRY RUN — upcoming IPOs in the next {lookahead} days (no AI, no Discord)")
    print("=" * 70)

    # 1) Facts: Groww -> IPOAlerts -> NSE+BSE
    try:
        ipos = IPOFetcher(
            os.getenv("FALLBACK_API_KEY", "")
        ).fetch_upcoming(lookahead)
    except AllProvidersFailed as exc:
        print("\n[FACTS] All facts providers failed to return data:\n")
        print(f"  {exc}\n")
        print("Tip: NSE/BSE block automated/datacenter traffic. Run this from a")
        print("home connection, and check the note in fetcher.py about the BSE fields.")
        return 1

    if not ipos:
        print("\nNo upcoming IPOs found in the window (or the feed was empty).")
        return 0

    facts_source = ipos[0].source.upper() if ipos else "?"
    print(f"\n[FACTS] {len(ipos)} IPO(s) from {facts_source}.")

    # 2) GMP: aggregator API (never fatal; skipped if facts already supplied it)
    matched = 0
    try:
        matched = GMPResolver(
            os.getenv("PRIMARY_API_KEY", ""),
            os.getenv("FALLBACK_API_KEY", ""),
        ).attach(ipos)
    except Exception as exc:  # noqa: BLE001
        print(f"[GMP]   skipped ({exc!r})")
    print(f"[GMP]   matched to {matched}/{len(ipos)} IPO(s).\n")

    # 3) Print the merged table
    for i, ipo in enumerate(ipos, 1):
        sym = f"  ({ipo.symbol})" if ipo.symbol else ""
        print(f"[{i}] {ipo.name}{sym}")
        print(f"    Opens : {ipo.expected_date or 'TBD':<12} Closes: {ipo.close_date or 'TBD':<12} Status: {ipo.status or 'N/A'}")
        line3 = f"    Price : {ipo.price_range:<12}"
        if ipo.lot_size:
            line3 += f" Lot: {ipo.lot_size}"
        if ipo.issue_size:
            line3 += f"   Issue size: {ipo.issue_size}"
        if ipo.subscription:
            line3 += f"   Subscribed: {ipo.subscription}"
        print(line3)
        print(f"    GMP   : {ipo.gmp or '—':<12} Exchange: {ipo.exchange or 'N/A'}   Source: {ipo.source}")
        if ipo.notes:
            note = ipo.notes.replace("\n", " ")
            print(f"    Notes : {note[:110]}{'…' if len(note) > 110 else ''}")
        print()

    print("=" * 70)
    print(f"Done. {len(ipos)} IPO(s), GMP on {matched}. Nothing was sent or stored.")
    print("=" * 70)
    return 0
