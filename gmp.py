"""
gmp.py
======
Grey-Market Premium (GMP) + issue-price acquisition. There is NO official source
for GMP (it is an unofficial grey-market number). Sources, in priority order:

    1. IPOWatchScraper       (primary, keyless) -> ipowatch.in's live GMP table
    2. AggregatorGMPProvider (fallback)          -> IPOGuru / IPOAlerts JSON APIs

IPOWatch is checked first because it's dedicated to tracking GMP specifically
(the aggregator APIs cover it as an afterthought and, in practice, rarely have
data for the actual IPOs this project fetches) and because its page is plain
server-rendered HTML — no JS rendering, no key, no rate limit seen so far. Same
caveat as any unofficial scrape: it can break if the site restructures.

This step only runs at all when `fetcher.py`'s facts provider didn't already
supply GMP (e.g. IPOAlerts-as-facts-source already includes it on every row —
see `_gmp_from_ipoalerts_row` in fetcher.py — so this module is skipped rather
than making a second, redundant call to the same API).

Each source returns a {normalized_company_name: GMPEntry} map. `GMPEntry` carries
both the GMP string and the issue price — the latter matters because NSE's own
feed doesn't expose a price band, so we backfill it from here.

GMP/price is enrichment, never a hard dependency: if every source fails, the
pipeline continues without it. `GMPResolver.attach(ipos)` stamps `ipo.gmp` (and
`ipo.price_*` when the exchange didn't provide one) onto each matching IPO.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import requests

from fetcher import IPO, _format_gmp_obj, _parse_band, build_session
from matching import best_match, normalize_company_name

log = logging.getLogger(__name__)

_TIMEOUT = (6, 20)

_GMP_BLANK = {"", "-", "--", "0", "₹0", "n/a", "na"}


def _clean_gmp_text(text: str) -> str:
    text = (text or "").strip()
    return "" if text.lower() in _GMP_BLANK else text


@dataclass
class GMPEntry:
    gmp: str = ""
    price_low: Optional[float] = None
    price_high: Optional[float] = None
    price_raw: str = ""
    est_listing: str = ""


class GMPSource:
    name = "base"

    def available(self) -> bool:
        return True

    def fetch(self) -> dict[str, GMPEntry]:  # pragma: no cover
        raise NotImplementedError


class IPOWatchScraper(GMPSource):
    """Primary — ipowatch.in's live GMP table, keyless. Plain server-rendered
    HTML (unlike InvestorGain, which moved its GMP table to client-side JS
    rendering and can no longer be scraped at all — see fetcher.py's history
    with that source for the same failure mode)."""

    name = "ipowatch"
    _URL = "https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/"

    def __init__(self, session: requests.Session) -> None:
        self.session = session

    def fetch(self) -> dict[str, GMPEntry]:
        resp = self.session.get(
            self._URL,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return self._parse(resp.text)

    @staticmethod
    def _parse(html: str) -> dict[str, GMPEntry]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            headers = [c.get_text(strip=True).lower() for c in rows[0].find_all(["td", "th"])]
            if "ipo name" not in headers or "ipo gmp" not in headers:
                continue  # not the GMP table — this page has other tables too

            name_i = headers.index("ipo name")
            gmp_i = headers.index("ipo gmp")
            est_i = headers.index("est. listing") if "est. listing" in headers else None

            result: dict[str, GMPEntry] = {}
            for tr in rows[1:]:
                cells = tr.find_all(["td", "th"])
                if len(cells) <= max(name_i, gmp_i):
                    continue
                key = normalize_company_name(cells[name_i].get_text(" ", strip=True))
                gmp = _clean_gmp_text(cells[gmp_i].get_text(strip=True))
                if not key or not gmp:
                    continue
                est = cells[est_i].get_text(" ", strip=True) if est_i is not None and est_i < len(cells) else ""
                result[key] = GMPEntry(gmp=gmp, est_listing=est)
            if result:
                return result
        raise RuntimeError("IPOWatch: no GMP rows parsed (page structure may have changed)")


class AggregatorGMPProvider(GMPSource):
    """Fallback — pull GMP + price from the IPOGuru / IPOAlerts JSON APIs."""

    name = "aggregator-api"
    _IPOGURU = "https://www.ipoguru.in/api/v1/ipos"
    _IPOALERTS = "https://api.ipoalerts.in/ipos"

    def __init__(self, ipoguru_key: str, ipoalerts_key: str, session: requests.Session) -> None:
        self.ipoguru_key = ipoguru_key
        self.ipoalerts_key = ipoalerts_key
        self.session = session

    def available(self) -> bool:
        return bool(self.ipoguru_key or self.ipoalerts_key)

    def fetch(self) -> dict[str, GMPEntry]:
        errors: list[str] = []
        for label, fn, key in (
            ("ipoguru", self._from_ipoguru, self.ipoguru_key),
            ("ipoalerts", self._from_ipoalerts, self.ipoalerts_key),
        ):
            if not key:
                continue
            try:
                data = fn(key)
                if data:
                    log.info("GMP aggregator '%s' returned %d entries", label, len(data))
                    return data
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{label}: {exc!r}")
                log.warning("GMP aggregator '%s' failed: %r", label, exc)
        raise RuntimeError(
            "All GMP aggregators failed: " + " ; ".join(errors)
            if errors else "No aggregator keys configured"
        )

    def _from_ipoguru(self, key: str) -> dict[str, GMPEntry]:
        resp = self.session.get(
            self._IPOGURU,
            params={"status": "upcoming"},
            headers={"X-API-KEY": key, "Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        out: dict[str, GMPEntry] = {}
        for row in resp.json().get("data", []) or []:
            gmp = row.get("gmp") or {}
            gstr = _format_gmp_obj(gmp.get("price"), gmp.get("percentage")) if isinstance(gmp, dict) else ""
            lo, hi, raw = _parse_band(row.get("price_band") or row.get("issue_price"))
            k = normalize_company_name(row.get("name", ""))
            if k and (gstr or lo):
                out[k] = GMPEntry(gmp=gstr, price_low=lo, price_high=hi, price_raw=raw)
        return out

    def _from_ipoalerts(self, key: str) -> dict[str, GMPEntry]:
        # Free-tier IPOAlerts keys only support status=open (see fetcher.py's
        # IPOAlertsFactProvider) — "upcoming" would just fail on those plans.
        resp = self.session.get(
            self._IPOALERTS,
            params={"status": "open"},
            headers={"x-api-key": key, "Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload if isinstance(payload, list) else (payload.get("ipos") or payload.get("data") or [])
        out: dict[str, GMPEntry] = {}
        for row in rows:
            gmp = row.get("gmp") or {}
            if isinstance(gmp, dict):
                gstr = _format_gmp_obj(gmp.get("premium") or gmp.get("price"),
                                       gmp.get("percentage") or gmp.get("gainPercent"))
            else:
                gstr = str(gmp).strip()
            lo, hi, raw = _parse_band(row.get("priceRange"))
            k = normalize_company_name(row.get("name", ""))
            if k and (gstr or lo):
                out[k] = GMPEntry(gmp=gstr, price_low=lo, price_high=hi, price_raw=raw)
        return out


class GMPResolver:
    """Resolves GMP+price from the first working source and attaches to IPOs."""

    def __init__(
        self,
        ipoguru_key: str,
        ipoalerts_key: str,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.session = session or build_session()
        self.sources: list[GMPSource] = [
            IPOWatchScraper(self.session),
            AggregatorGMPProvider(ipoguru_key, ipoalerts_key, self.session),
        ]

    def resolve(self) -> dict[str, GMPEntry]:
        for source in self.sources:
            if not source.available():
                log.info("Skipping GMP source '%s' (not configured)", source.name)
                continue
            try:
                log.info("Fetching GMP from '%s'", source.name)
                data = source.fetch()
                if data:
                    log.info("GMP source '%s' provided %d entries", source.name, len(data))
                    return data
            except Exception as exc:  # noqa: BLE001 - GMP is optional; try next source
                log.warning("GMP source '%s' failed: %r — falling back", source.name, exc)
                continue
        log.warning("No GMP source succeeded — proceeding without grey-market data.")
        return {}

    def attach(self, ipos: list[IPO]) -> int:
        """Stamp gmp (and backfill price) onto matching IPOs; returns GMP matches."""
        gmp_map = self.resolve()
        if not gmp_map:
            return 0
        matched = 0
        for ipo in ipos:
            entry = best_match(ipo.name, gmp_map)
            if entry is None and ipo.symbol:
                entry = best_match(ipo.symbol, gmp_map)
            if entry is None:
                continue
            if entry.gmp and not ipo.gmp:  # don't overwrite GMP the facts source gave
                ipo.gmp = entry.gmp
                matched += 1
            # Backfill price only when the exchange didn't give us one.
            if ipo.price_low is None and entry.price_low is not None:
                ipo.price_low = entry.price_low
                ipo.price_high = entry.price_high
                ipo.price_band_raw = entry.price_raw
        log.info("Attached GMP to %d/%d IPO(s)", matched, len(ipos))
        return matched
