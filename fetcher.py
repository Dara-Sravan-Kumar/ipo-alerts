"""
fetcher.py
==========
FACTUAL IPO data acquisition (dates, price band, lot size, issue size, and — when
the source has it — GMP) from a prioritized list of providers.

Fallback hierarchy:
    1. Groww     (primary, keyless) -> scrapes groww.in/ipo's embedded JSON
    2. IPOAlerts (fallback, when FALLBACK_API_KEY is set) -> aggregates NSE + BSE
    3. NSE + BSE (last resort, aggregated) -> keyless, official exchange APIs

Groww tracks companies from the DRHP/RHP filing stage (as soon as a company
files with SEBI), which is days ahead of NSE/BSE's own "upcoming issues" feeds
— those only populate once a listing is closer to finalized on the exchange's
end. IPOAlerts and NSE+BSE exist as fallbacks in case Groww restructures their
page (same risk every scraped-but-undocumented source in this file carries).

`IPOFetcher` walks the providers in priority order; if one raises *any*
exception (NSE frequently blocks/rate-limits automated traffic) it is logged and
the next provider is tried immediately. Only if every provider fails does it
raise `AllProvidersFailed`.

NSE/BSE never supply GMP (there's no official source for it) — `gmp.py` fills
that in separately via the same IPOAlerts key or the IPOGuru API, matched onto
these records by company name in `main.py`.

Note: NSE and BSE expose *undocumented internal* JSON endpoints. They require
browser-like headers and, for NSE, a cookie handshake (hit the site once to get
cookies, then call the API on the same session). They also block datacenter IPs,
so this works reliably from a residential connection but may be blocked from a
cloud host. Field names below are mapped tolerantly and should be sanity-checked
against a live response the first time you run it.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from matching import normalize_company_name

log = logging.getLogger(__name__)

_TIMEOUT = (6, 20)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


class AllProvidersFailed(RuntimeError):
    """Raised when every configured factual-data provider fails."""


@dataclass
class IPO:
    """Normalized, provider-agnostic IPO record (Indian mainboard/SME)."""

    name: str
    source: str
    symbol: str = ""
    exchange: str = ""
    sector: str = ""
    expected_date: str = ""       # IPO open date, ISO (YYYY-MM-DD), best-effort
    close_date: str = ""
    status: str = ""
    price_low: Optional[float] = None
    price_high: Optional[float] = None
    price_band_raw: str = ""
    currency: str = "INR"
    lot_size: Optional[int] = None
    issue_size: str = ""
    subscription: str = ""        # e.g. "6.57x" (times subscribed) — from NSE
    gmp: str = ""                 # filled later by the GMP merge step
    notes: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def dedup_key(self) -> str:
        ident = (self.symbol or self.name).strip().upper()
        return f"{ident}|{self.expected_date}|{self.status}".lower()

    @property
    def _symbol_prefix(self) -> str:
        return "₹" if self.currency.upper() == "INR" else f"{self.currency} "

    @property
    def price_range(self) -> str:
        sym = self._symbol_prefix
        if self.price_low and self.price_high:
            if self.price_low == self.price_high:
                return f"{sym}{self.price_low:g}"
            return f"{sym}{self.price_low:g}–{self.price_high:g}"
        if self.price_high:
            return f"{sym}~{self.price_high:g}"
        return self.price_band_raw or "TBD"

    @property
    def financial_overview(self) -> str:
        parts = [f"Exchange: {self.exchange or 'N/A'}"]
        if self.symbol:
            parts.append(f"Symbol: {self.symbol}")
        if self.sector:
            parts.append(f"Type/Sector: {self.sector}")
        parts.append(f"Price band: {self.price_range}")
        if self.lot_size:
            parts.append(f"Lot size: {self.lot_size}")
        if self.issue_size:
            parts.append(f"Issue size: {self.issue_size}")
        if self.subscription:
            parts.append(f"Subscription: {self.subscription} (times subscribed)")
        if self.gmp:
            parts.append(f"Grey Market Premium (GMP): {self.gmp}")
        parts.append(f"Status: {self.status or 'N/A'}")
        parts.append(f"Opens: {self.expected_date or 'N/A'}")
        if self.close_date:
            parts.append(f"Closes: {self.close_date}")
        overview = " | ".join(parts)
        if self.notes:
            overview += f"\n\nCompany notes:\n{self.notes[:1500]}"
        return overview


def build_session() -> requests.Session:
    """A requests session with pooling, retries and a browser User-Agent."""
    session = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.6,
        status_forcelist=(500, 502, 503, 504),  # NOT 429 — handle rate limits ourselves
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
        # Do NOT obey a server's Retry-After (can be 60s+, causing multi-minute hangs).
        respect_retry_after_header=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": _BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"})
    return session


def _format_gmp_obj(price: Any, pct: Any) -> str:
    """Render a {price, percentage} GMP pair (however a provider splits it) as
    a single display string, e.g. '₹45 (20%)'. Shared by fetcher.py and gmp.py
    since both talk to the IPOAlerts API and get GMP in this shape."""
    if price in (None, "", 0) and pct in (None, "", 0):
        return ""
    out = f"₹{price}" if price not in (None, "") else ""
    if pct not in (None, ""):
        out += f" ({pct}%)" if out else f"{pct}%"
    return out.strip()


# Different providers use different words for the same real-world state (NSE's
# "Active" vs everyone else's "open"). IPO.dedup_key includes status, so if two
# providers describe the identical currently-open issue with different words,
# switching which provider wins between runs produces a different dedup key —
# and a duplicate Discord alert for something already sent. Canonicalize here.
_STATUS_SYNONYMS = {"active": "open", "trading": "open"}


def _normalize_status(raw: Any, default: str) -> str:
    status = (str(raw).strip() if raw else default).lower()
    return _STATUS_SYNONYMS.get(status, status)


def _to_int(value: Any) -> Optional[int]:
    try:
        if value in (None, ""):
            return None
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


_PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _parse_band(raw: Any) -> tuple[Optional[float], Optional[float], str]:
    """Parse '95 - 100', '₹95-100', '100 to 105' or a single value."""
    if raw in (None, ""):
        return None, None, ""
    text = str(raw)
    nums = [float(n) for n in _PRICE_RE.findall(text)]
    if not nums:
        return None, None, text.strip()
    if len(nums) == 1:
        return nums[0], nums[0], text.strip()
    return min(nums[:2]), max(nums[:2]), text.strip()


def _norm_date(value: Any) -> str:
    """Best-effort normalization of a provider date to ISO YYYY-MM-DD."""
    if not value:
        return ""
    text = str(value).strip()
    core = text[:19] if "T" in text else text
    for fmt in (
        "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d-%b-%Y", "%d-%B-%Y",
        "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y",
    ):
        try:
            return datetime.strptime(core, fmt).date().isoformat()
        except ValueError:
            continue
    return text[:10]


# --------------------------------------------------------------------------- #
# Provider interface + implementations
# --------------------------------------------------------------------------- #
class FactProvider:
    """Base class for factual (exchange) IPO data providers."""

    name = "base"

    def __init__(self, session: requests.Session) -> None:
        self.session = session

    def available(self) -> bool:
        return True  # exchange sources need no API key

    def fetch(self, start: date, end: date) -> list[IPO]:  # pragma: no cover
        raise NotImplementedError


_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


def _epoch_ms_to_date(value: Any) -> str:
    """Groww timestamps are epoch milliseconds; convert to ISO YYYY-MM-DD."""
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


class GrowwProvider(FactProvider):
    """Primary — groww.in/ipo, keyless. The page is server-rendered (Next.js);
    the full IPO list is embedded as clean structured JSON in a
    `<script id="__NEXT_DATA__">` tag, no headless browser needed.

    Tracks companies from the DRHP/RHP filing stage, so it surfaces IPOs days
    before NSE/BSE's own "upcoming issues" feeds do. `upcomingDataList` entries
    without a confirmed `bidStartTimestamp` yet (DRHP filed, no dates set) are
    skipped — we can't place them in a lookahead window if we don't know when
    they open, and including them would flood alerts with immature filings.
    """

    name = "groww"
    _URL = "https://groww.in/ipo"

    def fetch(self, start: date, end: date) -> list[IPO]:
        resp = self.session.get(
            self._URL,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        match = _NEXT_DATA_RE.search(resp.text)
        if not match:
            raise RuntimeError("Groww: __NEXT_DATA__ block not found (page structure may have changed)")
        page_props = json.loads(match.group(1))["props"]["pageProps"]

        results: list[IPO] = []
        for row in page_props.get("openDataList") or []:
            results.append(self._map_open(row))
        for row in page_props.get("upcomingDataList") or []:
            if not row.get("bidStartTimestamp"):
                continue  # DRHP-stage only, no confirmed date — nothing to schedule yet
            results.append(self._map_upcoming(row))
        return results

    @staticmethod
    def _price_and_lot(row: dict[str, Any]) -> tuple[Optional[float], Optional[float], Optional[int]]:
        categories = row.get("categories") or []
        if not categories:
            return None, None, None
        cat = categories[0]
        return cat.get("minPrice"), cat.get("maxPrice"), _to_int(cat.get("lotSize"))

    @classmethod
    def _map_open(cls, row: dict[str, Any]) -> IPO:
        lo, hi, lot = cls._price_and_lot(row)
        subs = row.get("overallSubscription") or 0
        return IPO(
            name=(row.get("companyName") or "").strip(),
            source="groww",
            symbol=(row.get("symbol") or "").strip(),
            exchange="NSE/BSE",
            sector="SME" if row.get("isSme") else "Mainboard",
            expected_date=_epoch_ms_to_date(row.get("bidStartTimestamp")),
            close_date=_epoch_ms_to_date(row.get("bidEndTimestamp")),
            status="open",
            price_low=lo,
            price_high=hi,
            lot_size=lot,
            subscription=f"{subs:g}x" if subs else "",
            raw=row,
        )

    @staticmethod
    def _map_upcoming(row: dict[str, Any]) -> IPO:
        return IPO(
            name=(row.get("companyName") or "").strip(),
            source="groww",
            symbol=(row.get("symbol") or "").strip(),
            exchange="NSE/BSE",
            sector="SME" if row.get("isSme") else "Mainboard",
            expected_date=_epoch_ms_to_date(row.get("bidStartTimestamp")),
            status="forthcoming",
            raw=row,
        )


class NSEProvider(FactProvider):
    """Primary — National Stock Exchange (keyless internal JSON API)."""

    name = "nse"
    _HOME = "https://www.nseindia.com/market-data/all-upcoming-issues-ipo"
    _UPCOMING = "https://www.nseindia.com/api/all-upcoming-issues?category=ipo"
    _CURRENT = "https://www.nseindia.com/api/ipo-current-issue"

    def fetch(self, start: date, end: date) -> list[IPO]:
        self._prime_cookies()
        merged: dict[str, IPO] = {}

        for row in self._get_json(self._UPCOMING):
            ipo = self._map(row, has_price=False)
            if ipo:
                merged[ipo.symbol or ipo.name] = ipo

        # The current-issue feed is richer (price band, size); let it win.
        for row in self._get_json(self._CURRENT):
            ipo = self._map(row, has_price=True)
            if ipo:
                merged[ipo.symbol or ipo.name] = ipo

        return list(merged.values())

    def _prime_cookies(self) -> None:
        # NSE hands out session cookies only after you load a real page first.
        resp = self.session.get(
            self._HOME,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()

    def _get_json(self, url: str) -> list[dict[str, Any]]:
        resp = self.session.get(
            url,
            headers={"Accept": "application/json, text/plain, */*", "Referer": self._HOME},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict):
            payload = payload.get("data") or payload.get("upcomingIssues") or []
        return payload or []

    @staticmethod
    def _map(row: dict[str, Any], *, has_price: bool) -> Optional[IPO]:
        name = (row.get("companyName") or row.get("name") or "").strip()
        symbol = (row.get("symbol") or "").strip()
        if not (name or symbol):
            return None
        lo = hi = None
        band = ""
        if has_price:
            lo, hi, band = _parse_band(row.get("issuePrice") or row.get("priceBand"))
        is_bse = str(row.get("isBse", "")).strip() in {"1", "true", "True", "Y"}
        subs = str(row.get("noOfTime") or "").strip()
        return IPO(
            name=name or symbol,
            source="nse",
            symbol=symbol,
            exchange="NSE, BSE" if is_bse else "NSE",
            sector=(row.get("series") or "").strip(),
            expected_date=_norm_date(row.get("issueStartDate")),
            close_date=_norm_date(row.get("issueEndDate")),
            status=_normalize_status(row.get("status"), "upcoming"),
            price_low=lo,
            price_high=hi,
            price_band_raw=band,
            lot_size=_to_int(row.get("lotSize") or row.get("bidLotSize") or row.get("marketLot")),
            issue_size=str(row.get("issueSize") or "").strip(),
            subscription=f"{subs}x" if subs else "",
            raw=row,
        )


class BSEProvider(FactProvider):
    """
    Bombay Stock Exchange (keyless internal JSON API).

    The endpoint this project shipped with (`GetPublicIssueData/w`) now 302s to
    an error page — dead. The real one, found by reading BSE's own app bundle
    (`GetPublicIssue/w`, no params needed), returns EVERY public-issue type:
    IPO, OFS (offer-for-sale), RI (rights issue), OTB (buyback tender) — only
    `IR_flag == "IPO"` rows are kept here, since the other three are capital
    actions on already-listed companies, not IPOs.

    Note: this feed only covers BSE mainboard public issues. BSE-SME-platform
    IPOs (e.g. dual-listed NSE/BSE SME issues) don't appear here — NSE's feed
    is still the source for those.
    """

    name = "bse"
    _URL = "https://api.bseindia.com/BseIndiaAPI/api/GetPublicIssue/w"
    _HOME = "https://www.bseindia.com/publicissue.html"

    # BSE's Status codes aren't documented; going by observed data, "F" (not yet
    # open) and "L" (already open, ongoing) look like Forthcoming/Live rather
    # than "listed" — deliberately NOT mapped to a word _post_process excludes.
    _STATUS = {"f": "forthcoming", "l": "open"}

    def fetch(self, start: date, end: date) -> list[IPO]:
        resp = self.session.get(
            self._URL,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": self._HOME,
                "Origin": "https://www.bseindia.com",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        rows = (resp.json() or {}).get("Table") or []
        results: list[IPO] = []
        for row in rows:
            if (row.get("IR_flag") or "").strip().upper() != "IPO":
                continue  # skip OFS / RI / OTB — capital actions, not IPOs
            name = (row.get("Scrip_Name") or "").strip()
            if not name:
                continue
            lo, hi, band = _parse_band(row.get("Price_Band"))
            status_code = (row.get("Status") or "").strip().lower()
            results.append(
                IPO(
                    name=name,
                    source="bse",
                    symbol=str(row.get("Scrip_cd") or "").strip(),
                    exchange="BSE",
                    expected_date=_norm_date(row.get("Start_Dt")),
                    close_date=_norm_date(row.get("End_Dt")),
                    status=self._STATUS.get(status_code, status_code or "upcoming"),
                    price_low=lo,
                    price_high=hi,
                    price_band_raw=band,
                    raw=row,
                )
            )
        return results


class IPOAlertsFactProvider(FactProvider):
    """
    Primary (when a key is set) — IPOAlerts aggregates BOTH NSE and BSE, so it
    lists every live IPO, not just NSE mainboard/SME. Free plan only supports
    status=open and returns 1 record per page, so we paginate.
    """

    name = "ipoalerts"
    _URL = "https://api.ipoalerts.in/ipos"
    _MAX_PAGES = 40
    _MAX_429_RETRIES = 4   # per page
    _429_WAIT = 4.0        # seconds to back off on a rate-limit

    def __init__(self, api_key: str, session: requests.Session) -> None:
        super().__init__(session)
        self.api_key = api_key

    def available(self) -> bool:
        return bool(self.api_key)

    def fetch(self, start: date, end: date) -> list[IPO]:
        headers = {"x-api-key": self.api_key, "Accept": "application/json"}
        rows: list[dict[str, Any]] = []
        page, pages, tries = 1, 1, 0
        while page <= pages and page <= self._MAX_PAGES:
            resp = self.session.get(
                self._URL,
                params={"status": "open", "page": page},
                headers=headers,
                timeout=_TIMEOUT,
            )

            # Rate limited: back off and retry the SAME page a few times. The free
            # plan throttles aggressively when paginating, so this spreads the
            # requests out instead of losing the rest of the list.
            if resp.status_code == 429:
                tries += 1
                if tries <= self._MAX_429_RETRIES:
                    log.info("IPOAlerts rate-limited on page %d; backing off %.0fs (try %d/%d)",
                             page, self._429_WAIT, tries, self._MAX_429_RETRIES)
                    time.sleep(self._429_WAIT)
                    continue
                if page == 1:
                    resp.raise_for_status()  # got nothing -> fall back to NSE
                log.warning("IPOAlerts still rate-limited on page %d; using %d rows so far",
                            page, len(rows))
                break
            tries = 0

            # If the first page fails for another reason, surface it (-> NSE).
            # If a LATER page fails, keep what we already collected.
            if resp.status_code != 200:
                if page == 1:
                    resp.raise_for_status()
                log.warning("IPOAlerts page %d returned %d; using %d rows so far",
                            page, resp.status_code, len(rows))
                break
            payload = resp.json()
            batch = payload.get("ipos") or payload.get("data") or []
            if not batch:
                break
            rows.extend(batch)
            pages = (payload.get("meta") or {}).get("totalPages", 1) or 1
            page += 1
            if page <= pages:
                time.sleep(0.8)  # be polite; avoid free-plan rate limiting

        results: list[IPO] = []
        for row in rows:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            lo, hi, band = _parse_band(row.get("priceRange"))
            exch = (row.get("source") or "").strip().upper()
            results.append(
                IPO(
                    name=name,
                    source="ipoalerts",
                    symbol=(row.get("symbol") or "").strip(),
                    exchange=exch or "NSE/BSE",
                    sector=(row.get("type") or "").strip(),
                    expected_date=_norm_date(row.get("startDate")),
                    close_date=_norm_date(row.get("endDate")),
                    status=_normalize_status(row.get("status"), "open"),
                    price_low=lo,
                    price_high=hi,
                    price_band_raw=band,
                    lot_size=_to_int(row.get("minQty")),
                    issue_size=str(row.get("issueSize") or "").strip(),
                    gmp=_gmp_from_ipoalerts_row(row),
                    notes=_notes_from(row),
                    raw=row,
                )
            )
        return results


def _gmp_from_ipoalerts_row(row: dict[str, Any]) -> str:
    """IPOAlerts already includes GMP on every IPO row — extract it here so the
    facts fetch doesn't need a second, separate GMP call to the same API."""
    gmp = row.get("gmp") or {}
    if isinstance(gmp, dict):
        return _format_gmp_obj(
            gmp.get("premium") or gmp.get("price"),
            gmp.get("percentage") or gmp.get("gainPercent"),
        )
    return str(gmp).strip()


def _notes_from(row: dict[str, Any]) -> str:
    chunks = []
    if row.get("strengths"):
        chunks.append("Strengths: " + "; ".join(map(str, row["strengths"][:5])))
    if row.get("risks"):
        chunks.append("Risks: " + "; ".join(map(str, row["risks"][:5])))
    return "\n".join(chunks)


# --------------------------------------------------------------------------- #
# Fetcher with fallback routing
# --------------------------------------------------------------------------- #
class IPOFetcher:
    """Facts sources, in priority order: Groww -> IPOAlerts -> NSE+BSE AGGREGATED.

    Groww is keyless and tracks companies from the DRHP filing stage, so it's
    the earliest and broadest source. IPOAlerts already aggregates NSE+BSE
    server-side, so when it's used it's used alone. If both fail, NSE and BSE
    are BOTH queried (not just "try NSE, only touch BSE if NSE totally failed")
    and merged by company name — each exchange only lists what's live on it,
    and they fill gaps in each other (e.g. NSE's SME feed lacks a price band
    for an issue that hasn't opened yet, which BSE's mainboard feed already has
    for the same company).

    InvestorGain used to be the primary source here (one scrape = whole list +
    GMP + price). It was removed: their site now renders that table entirely
    client-side via JS after page load, so a plain HTTP GET returns an empty
    "No data available" shell — there is no HTML left to scrape.
    """

    def __init__(
        self,
        ipoalerts_key: str = "",
        session: Optional[requests.Session] = None,
    ) -> None:
        self.session = session or build_session()
        self.groww = GrowwProvider(self.session)
        self.ipoalerts = IPOAlertsFactProvider(ipoalerts_key, self.session)
        self.nse = NSEProvider(self.session)
        self.bse = BSEProvider(self.session)

    def fetch_upcoming(self, lookahead_days: int) -> list[IPO]:
        start = date.today()
        end = start + timedelta(days=lookahead_days)

        for provider in (self.groww, self.ipoalerts):
            if not provider.available():
                log.info("Skipping facts provider '%s' (not configured)", provider.name)
                continue
            try:
                log.info("Fetching IPO facts from '%s'", provider.name)
                results = self._post_process(provider.fetch(start, end), start, end)
                log.info("Provider '%s' returned %d usable IPO(s)", provider.name, len(results))
                return results
            except Exception as exc:  # noqa: BLE001 - falls through to the next tier
                log.warning("Facts provider '%s' failed: %r — falling back", provider.name, exc)

        merged: dict[str, IPO] = {}
        errors: list[str] = []
        for provider in (self.nse, self.bse):
            try:
                log.info("Fetching IPO facts from '%s'", provider.name)
                rows = provider.fetch(start, end)
                log.info("Provider '%s' returned %d raw row(s)", provider.name, len(rows))
                self._merge_rows(merged, rows)
            except Exception as exc:  # noqa: BLE001 - the other exchange may still work
                msg = f"Facts provider '{provider.name}' failed: {exc!r}"
                log.warning(msg)
                errors.append(msg)

        if not merged:
            raise AllProvidersFailed(
                "All factual-data providers (Groww, IPOAlerts, NSE, BSE) failed. Details: " + " ; ".join(errors)
            )
        results = self._post_process(list(merged.values()), start, end)
        log.info("NSE+BSE aggregated: %d usable IPO(s)", len(results))
        return results

    @staticmethod
    def _merge_rows(bucket: dict[str, IPO], rows: list[IPO]) -> None:
        """Fold a provider's rows into `bucket`, keyed by normalized company
        name. The first source to mention a company wins the record; a later
        source only fills in fields the first source left blank (e.g. BSE
        supplying a price band NSE didn't have yet) — it never overwrites data
        that's already there."""
        for ipo in rows:
            key = normalize_company_name(ipo.name)
            if not key:
                continue
            existing = bucket.get(key)
            if existing is None:
                bucket[key] = ipo
                continue
            for field_name in ("price_low", "price_high", "price_band_raw", "lot_size",
                                "issue_size", "subscription", "gmp", "close_date", "sector"):
                if not getattr(existing, field_name) and getattr(ipo, field_name):
                    setattr(existing, field_name, getattr(ipo, field_name))
            if ipo.exchange and ipo.exchange not in existing.exchange:
                existing.exchange = f"{existing.exchange}, {ipo.exchange}" if existing.exchange else ipo.exchange
            if ipo.source not in existing.source:
                existing.source = f"{existing.source}+{ipo.source}"

    @staticmethod
    def _post_process(results: list[IPO], start: date, end: date) -> list[IPO]:
        today_s = start.isoformat()
        end_s = end.isoformat()
        seen: set[str] = set()
        cleaned: list[IPO] = []
        for ipo in results:
            if not ipo.name:
                continue
            if ipo.status in {"withdrawn", "canceled", "cancelled", "listed"}:
                continue
            # Expired: the issue already closed before today.
            if ipo.close_date and ipo.close_date < today_s:
                continue
            # Too far out: opens beyond the lookahead window.
            if ipo.expected_date and ipo.expected_date > end_s:
                continue
            if ipo.dedup_key in seen:
                continue
            seen.add(ipo.dedup_key)
            cleaned.append(ipo)
        cleaned.sort(key=lambda i: i.expected_date or "9999-99-99")
        return cleaned
