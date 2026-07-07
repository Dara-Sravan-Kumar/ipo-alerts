"""
main.py
=======
Entry point. Wires config -> fetcher -> analyzer -> notifier -> state store,
and schedules the whole thing to run daily.

Run once (useful for testing):
    python main.py --once

Run as a long-lived scheduler (default):
    python main.py
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from analyzer import Analyzer
from config import Config, ConfigError, load_config
from database import StateStore
from fetcher import AllProvidersFailed, IPOFetcher
from gmp import GMPResolver
from notifier import Notifier

log = logging.getLogger("ipo_tracker")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def run_check(config: Config) -> None:
    """A single end-to-end pass: fetch, analyze, notify new IPOs."""
    log.info("=== IPO check starting ===")

    fetcher = IPOFetcher(config.fallback_api_key)
    notifier = Notifier.from_config(config)
    store = StateStore(config.db_path)

    # 1) Fetch FACTS (Groww, else IPOAlerts, else NSE+BSE aggregated). If all fail, alert and bail.
    try:
        ipos = fetcher.fetch_upcoming(config.lookahead_days)
    except AllProvidersFailed as exc:
        log.error("All factual-data providers failed: %s", exc)
        notifier.send_critical_error(
            "Could not fetch IPO data — Groww, IPOAlerts, NSE and BSE all failed.\n\n"
            f"```{str(exc)[:1500]}```\n"
            "The system is still alive and will retry on the next schedule."
        )
        return

    if not ipos:
        log.info("No upcoming IPOs in the lookahead window.")
        return

    # 2) Enrich with GMP (aggregator API). Never fatal — best effort only.
    #    Skipped when the facts source already supplied GMP (e.g. IPOAlerts).
    if any(not ipo.gmp for ipo in ipos):
        try:
            GMPResolver(
                config.primary_api_key,
                config.fallback_api_key,
            ).attach(ipos)
        except Exception as exc:  # noqa: BLE001 - GMP must never break the run
            log.warning("GMP enrichment skipped: %r", exc)

    # 3) Analyze + notify only the ones we haven't sent before.
    analyzer = Analyzer(config.ai_provider, config.ai_api_key, config.ai_model)

    new_count = 0
    for ipo in ipos:
        if store.already_sent(ipo.dedup_key):
            log.debug("Skipping already-sent IPO: %s", ipo.dedup_key)
            continue

        try:
            analysis = analyzer.analyze(ipo)
            notifier.send_ipo(ipo, analysis)
            store.mark_sent(
                ipo.dedup_key,
                symbol=ipo.symbol,
                name=ipo.name,
                status=ipo.status,
                source=ipo.source,
            )
            new_count += 1
            # Be gentle with Discord + AI rate limits.
            time.sleep(1.0)
        except Exception as exc:  # noqa: BLE001 - one bad IPO shouldn't kill the run
            log.error("Failed to process IPO %s: %r", ipo.symbol or ipo.name, exc)
            continue

    log.info("=== IPO check complete: %d new notification(s) sent ===", new_count)


def run_digest(config: Config) -> None:
    """Fetch all live/upcoming IPOs and post ONE at-a-glance table to Discord."""
    log.info("=== IPO digest starting ===")
    notifier = Notifier.from_config(config)

    try:
        ipos = IPOFetcher(
            config.fallback_api_key
        ).fetch_upcoming(config.lookahead_days)
    except AllProvidersFailed as exc:
        log.error("All factual-data providers failed: %s", exc)
        notifier.send_critical_error(f"Could not fetch IPO data:\n```{str(exc)[:1500]}```")
        return

    if not ipos:
        log.info("No live/upcoming IPOs.")
        notifier.send_info("No live or upcoming IPOs in the window right now.")
        return

    # Only fetch GMP separately if the facts source didn't already provide it
    # (IPOAlerts does; NSE/BSE don't).
    if any(not ipo.gmp for ipo in ipos):
        try:
            GMPResolver(
                config.primary_api_key, config.fallback_api_key
            ).attach(ipos)
        except Exception as exc:  # noqa: BLE001
            log.warning("GMP enrichment skipped: %r", exc)

    # ONE batched AI call for the whole list (fast + cheap), not one per IPO.
    analyzer = Analyzer(config.ai_provider, config.ai_api_key, config.ai_model)
    analyses = analyzer.analyze_batch(ipos)
    items = list(zip(ipos, analyses))

    notifier.send_digest(items)
    log.info("=== IPO digest complete: %d IPO(s) in one table ===", len(items))


def _run_once_safely(config: Config) -> None:
    """Wrapper used by the scheduler so a crash never kills the process.

    The daily job sends the at-a-glance digest (one table of all live IPOs).
    Use `python main.py --once` for the detailed one-embed-per-IPO version.
    """
    try:
        run_digest(config)
    except Exception as exc:  # noqa: BLE001 - top-level guard
        log.exception("Unexpected error during scheduled run")
        try:
            Notifier.from_config(config).send_critical_error(
                f"Unexpected error during scheduled run:\n```{str(exc)[:1500]}```"
            )
        except Exception:  # noqa: BLE001
            log.error("Could not even report the error to Discord.")


def main() -> int:
    parser = argparse.ArgumentParser(description="IPO Tracker & AI Analysis")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check and exit (instead of starting the scheduler).",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Send sample IPO embeds to Discord using ONLY Discord creds "
        "(no IPO/AI keys needed). Great for a first test.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch live IPO facts + GMP and PRINT them to the terminal. "
        "No AI, no Discord, no state writes.",
    )
    parser.add_argument(
        "--digest",
        action="store_true",
        help="Post ONE at-a-glance table of ALL live/upcoming IPOs to Discord "
        "(not one embed per IPO).",
    )
    args = parser.parse_args()

    _setup_logging()

    if args.demo:
        from demo import run_demo

        return run_demo()

    if args.dry_run:
        from dryrun import run_dry_run

        return run_dry_run()

    try:
        config = load_config()
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 2

    if args.digest:
        run_digest(config)
        return 0

    if args.once:
        run_check(config)
        return 0

    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler(timezone=config.timezone)
    scheduler.add_job(
        _run_once_safely,
        trigger="cron",
        hour=config.schedule_hour,
        minute=config.schedule_minute,
        args=[config],
        id="daily_ipo_check",
        max_instances=1,
        coalesce=True,
    )
    log.info(
        "Scheduler started — daily IPO check at %02d:%02d (%s). Lookahead: %d days.",
        config.schedule_hour,
        config.schedule_minute,
        config.timezone,
        config.lookahead_days,
    )

    if config.run_on_start:
        log.info("RUN_ON_START enabled — running an initial check now.")
        _run_once_safely(config)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
