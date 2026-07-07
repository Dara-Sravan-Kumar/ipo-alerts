"""
notifier.py
===========
Formats analysis results into clean Discord embeds and delivers them.

Two delivery modes, auto-detected:
    * BOT      — POST to the Bot API using DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID
                 (preferred when both are set)
    * WEBHOOK  — POST to a real DISCORD_WEBHOOK_URL (.../api/webhooks/{id}/{token})

The embed JSON is identical for both; only the URL and auth header differ.

Colour coding (from the AI risk_level):
    LOW    -> green   (high-potential / lower risk)
    MEDIUM -> yellow  (neutral)
    HIGH   -> red     (high-risk)

Also exposes `send_critical_error` so `main.py` can alert the channel when the
whole pipeline fails (e.g. every data provider is down) instead of dying silently.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from analyzer import Analysis
from fetcher import IPO

log = logging.getLogger(__name__)

_TIMEOUT = (5, 15)

# Discord embed colours (decimal ints).
_COLOR_GREEN = 0x2ECC71
_COLOR_YELLOW = 0xF1C40F
_COLOR_RED = 0xE74C3C
_COLOR_GREY = 0x95A5A6

_RISK_COLOR = {
    "LOW": _COLOR_GREEN,
    "MEDIUM": _COLOR_YELLOW,
    "HIGH": _COLOR_RED,
}

_RISK_EMOJI = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}


def _render_table(rows: list[tuple[str, str]]) -> str:
    """Render (label, value) pairs as an aligned two-column monospace table."""
    rows = [(k, str(v)) for k, v in rows if v not in (None, "")]
    if not rows:
        return "No data."
    width = max(len(k) for k, _ in rows)
    sep = "─" * (width + 2)
    lines = [f"{'Field'.ljust(width)}  Value", sep]
    lines += [f"{k.ljust(width)}  {v}" for k, v in rows]
    return "\n".join(lines)


_BOT_API = "https://discord.com/api/v10/channels/{channel_id}/messages"


class DiscordConfigError(RuntimeError):
    """Raised when no valid Discord delivery method is configured."""


class Notifier:
    def __init__(
        self,
        webhook_url: str = "",
        bot_token: str = "",
        channel_id: str = "",
    ) -> None:
        self.webhook_url = (webhook_url or "").strip()
        self.bot_token = (bot_token or "").strip()
        self.channel_id = str(channel_id or "").strip()
        self.session = requests.Session()

        if self.bot_token and self.channel_id:
            self.mode = "bot"
        elif "/api/webhooks/" in self.webhook_url:
            self.mode = "webhook"
        else:
            raise DiscordConfigError(
                "No valid Discord delivery configured. Set DISCORD_BOT_TOKEN + "
                "DISCORD_CHANNEL_ID, or a real DISCORD_WEBHOOK_URL "
                "(https://discord.com/api/webhooks/<id>/<token>). Note: a "
                "https://discord.com/channels/... link is the channel URL, not a webhook."
            )
        log.info("Discord delivery mode: %s", self.mode)

    @classmethod
    def from_config(cls, config: Any) -> "Notifier":
        return cls(
            webhook_url=getattr(config, "discord_webhook_url", ""),
            bot_token=getattr(config, "discord_bot_token", ""),
            channel_id=getattr(config, "discord_channel_id", ""),
        )

    # ------------------------------------------------------------------ #
    def send_ipo(self, ipo: IPO, analysis: Analysis) -> None:
        """Post a single IPO analysis embed."""
        self._post({"embeds": [self._build_embed(ipo, analysis)]})
        log.info("Posted Discord embed for %s", ipo.symbol or ipo.name)

    def send_digest(
        self,
        items: list[tuple[IPO, Any]],
        title: str = "🗓️ Live & Upcoming IPOs",
    ) -> None:
        """Post ONE detailed embed per IPO (the standard single-IPO format),
        ordered by risk: 🔴 HIGH first, 🟡 MEDIUM next, 🟢 LOW last.

        `items` is a list of (IPO, Analysis). Analysis drives both the card
        contents and the ordering.
        """
        order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        ordered = sorted(
            items, key=lambda t: order.get(getattr(t[1], "risk_level", ""), 3)
        )
        sent = 0
        for ipo, analysis in ordered:
            if analysis is None:
                continue
            self.send_ipo(ipo, analysis)
            sent += 1
            time.sleep(0.4)  # gentle pacing to avoid Discord rate limits
        log.info("Posted digest: %d IPO card(s), ordered red→yellow→green", sent)

    def send_critical_error(self, message: str) -> None:
        """Post a red 'critical error' embed so failures are visible."""
        embed = {
            "title": "🚨 IPO Tracker — Critical Error",
            "description": message[:4000],
            "color": _COLOR_RED,
            "footer": {"text": "IPO Tracker & AI Analysis"},
        }
        try:
            self._post({"embeds": [embed]})
            log.info("Posted critical error notification to Discord")
        except Exception as exc:  # noqa: BLE001 - last-resort path, must not raise
            log.error("Failed to post critical error to Discord: %r", exc)

    def send_info(self, message: str) -> None:
        """Post a neutral informational embed (e.g. 'no new IPOs today')."""
        embed = {
            "title": "ℹ️ IPO Tracker",
            "description": message[:4000],
            "color": _COLOR_GREY,
            "footer": {"text": "IPO Tracker & AI Analysis"},
        }
        self._post({"embeds": [embed]})

    # ------------------------------------------------------------------ #
    def _build_embed(self, ipo: IPO, analysis: Analysis) -> dict[str, Any]:
        color = _RISK_COLOR.get(analysis.risk_level, _COLOR_GREY)
        emoji = _RISK_EMOJI.get(analysis.risk_level, "⚪")

        title = f"{emoji} {ipo.name}"
        if ipo.symbol:
            title += f" (${ipo.symbol})"

        # 1) Compact facts + scored analysis rendered as an aligned table.
        table_rows = [
            ("Opens", ipo.expected_date or "TBD"),
            ("Closes", ipo.close_date or "TBD"),
            ("Status", (ipo.status or "N/A").title()),
            ("Price Band", ipo.price_range),
            ("Lot Size", str(ipo.lot_size) if ipo.lot_size else "—"),
            ("Issue Size", ipo.issue_size or "—"),
            ("Subscribed", ipo.subscription or "—"),
            ("GMP", ipo.gmp or "—"),
            ("Exchange", ipo.exchange or "N/A"),
            ("Sector", ipo.sector or "—"),
            ("Risk Level", analysis.risk_level),
            ("Hype Score", f"{analysis.hype_score}/100"),
            ("Hype vs Fund.", analysis.hype_vs_fundamentals),
        ]
        description = f"```\n{_render_table(table_rows)}\n```"

        # 2) The narrative analysis, also laid out as a labelled table.
        risks_text = "\n".join(f"• {r}" for r in analysis.potential_risks)
        analysis_rows = [
            ("📊 Market Sentiment", analysis.market_sentiment),
            ("⚠️ Potential Risks", risks_text),
            ("🧠 Analyst Take", analysis.summary),
        ]
        fields = [{"name": name, "value": (val or "—")[:1024], "inline": False}
                  for name, val in analysis_rows]

        footer = f"Risk: {analysis.risk_level} • Data: {ipo.source} • AI: {analysis.model}"
        if analysis.is_fallback:
            footer += " • ⚠️ AI fallback"

        return {
            "title": title[:256],
            "description": description[:4096],
            "color": color,
            "fields": fields,
            "footer": {"text": footer[:2048]},
        }

    def _post(self, payload: dict[str, Any]) -> None:
        if self.mode == "bot":
            url = _BOT_API.format(channel_id=self.channel_id)
            headers = {"Authorization": f"Bot {self.bot_token}"}
            resp = self.session.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
        else:
            resp = self.session.post(self.webhook_url, json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
