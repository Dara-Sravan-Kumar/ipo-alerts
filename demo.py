"""
demo.py
=======
Run the FULL notification pipeline using ONLY your Discord webhook.

No IPO API keys and no AI key are required. This uses a few hard-coded sample
IPOs and a simple rule-based ("heuristic") analysis instead of the AI engine,
so you can confirm the Discord embeds look right before wiring up the real keys.

Run it with:
    python main.py --demo
"""

from __future__ import annotations

import logging
import os
import re

from dotenv import load_dotenv

from analyzer import Analysis
from fetcher import IPO
from notifier import Notifier

log = logging.getLogger(__name__)


# Three sample IPOs chosen to show all three embed colours (green/yellow/red).
SAMPLE_IPOS: list[IPO] = [
    IPO(
        name="Stellar Green Energy Ltd",
        source="demo",
        exchange="NSE, BSE",
        sector="Mainboard / Renewables",
        expected_date="2026-07-10",
        close_date="2026-07-12",
        price_low=95,
        price_high=100,
        currency="INR",
        lot_size=150,
        issue_size="₹480 Cr",
        gmp="₹8 (8%)",
        notes="Profitable solar EPC player with strong order book and low debt.",
    ),
    IPO(
        name="NovaMart Retail Ltd",
        source="demo",
        exchange="NSE, BSE",
        sector="Mainboard / Retail",
        expected_date="2026-07-14",
        close_date="2026-07-16",
        price_low=210,
        price_high=225,
        currency="INR",
        lot_size=66,
        issue_size="₹1,200 Cr",
        gmp="₹45 (20%)",
        notes="Fast-growing retailer, but thin margins and rich valuation.",
    ),
    IPO(
        name="HyperAI Robotics Ltd",
        source="demo",
        exchange="NSE SME",
        sector="SME / AI Hardware",
        expected_date="2026-07-18",
        close_date="2026-07-20",
        price_low=140,
        price_high=140,
        currency="INR",
        lot_size=1000,
        issue_size="₹75 Cr",
        gmp="₹95 (68%)",
        notes="Pre-revenue AI hardware story. Huge GMP, no profits yet.",
    ),
]


def _gmp_percent(gmp: str) -> float:
    """Pull the percentage out of a GMP string like '₹95 (68%)'."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", gmp or "")
    return float(match.group(1)) if match else 0.0


def heuristic_analysis(ipo: IPO) -> Analysis:
    """A tiny rule-based stand-in for the AI engine (demo only)."""
    gmp_pct = _gmp_percent(ipo.gmp)

    if gmp_pct >= 40:
        risk, hype = "HIGH", 90
        sentiment = "Frothy — grey-market demand is running far ahead of fundamentals."
        verdict = "Mostly hype"
        risks = [
            "Very high GMP suggests speculative, momentum-driven interest.",
            "Valuation likely disconnected from current earnings.",
            "High risk of listing-day volatility and post-listing drawdown.",
        ]
    elif gmp_pct >= 15:
        risk, hype = "MEDIUM", 55
        sentiment = "Healthy but not euphoric — moderate premium expected on listing."
        verdict = "Balanced hype vs fundamentals"
        risks = [
            "Valuation is on the fuller side; limited margin of safety.",
            "Sector competition could pressure margins.",
        ]
    else:
        risk, hype = "LOW", 25
        sentiment = "Calm, fundamentals-led interest rather than speculative frenzy."
        verdict = "Fundamentals over hype"
        risks = [
            "Modest listing pop likely; not a quick-flip candidate.",
            "Returns depend on business execution over time.",
        ]

    return Analysis(
        market_sentiment=sentiment,
        potential_risks=risks,
        hype_vs_fundamentals=verdict,
        hype_score=hype,
        risk_level=risk,
        summary=(
            f"{ipo.name} opens {ipo.expected_date} at {ipo.price_range}. "
            f"{ipo.notes} (Demo heuristic — no AI was used.)"
        ),
        model="demo-heuristic",
        is_fallback=False,
    )


def run_demo() -> int:
    """Send the sample IPO embeds to Discord (bot token+channel OR webhook)."""
    load_dotenv()
    try:
        notifier = Notifier(
            webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
            bot_token=os.getenv("DISCORD_BOT_TOKEN", ""),
            channel_id=os.getenv("DISCORD_CHANNEL_ID", ""),
        )
    except Exception as exc:  # noqa: BLE001
        log.error("%s", exc)
        return 2

    log.info("DEMO MODE — sending %d sample IPO embeds to Discord.", len(SAMPLE_IPOS))
    for ipo in SAMPLE_IPOS:
        analysis = heuristic_analysis(ipo)
        notifier.send_ipo(ipo, analysis)
        log.info("Sent demo embed: %s (%s)", ipo.name, analysis.risk_level)

    log.info("Demo complete — check your Discord channel.")
    return 0
