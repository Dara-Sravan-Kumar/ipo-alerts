# 📈 IPO Tracker & AI Analysis → Discord

An automated system that fetches upcoming IPOs from financial data APIs, runs a
sharp AI analysis on each one (sentiment, risks, hype-vs-fundamentals), and posts
clean colour-coded embeds to a Discord channel — once a day, on schedule, with no
duplicate spam.

## Architecture

```
config.py     → loads & validates environment variables
database.py   → SQLite de-duplication (never send the same IPO twice)
fetcher.py    → FACTUAL IPO data from Groww → IPOAlerts → NSE+BSE (aggregated)
gmp.py        → GMP: IPOGuru / IPOAlerts aggregator API (only when facts didn't already supply it)
matching.py   → fuzzy company-name matching to merge GMP onto IPOs
analyzer.py   → AI analysis engine (OpenAI or Gemini)
notifier.py   → formats & fires colour-coded Discord embeds
main.py       → ties it together + APScheduler daily job
```

Data flow: `fetcher (facts) → gmp (enrich) → analyzer → notifier`, with
`database` gating duplicates and `config` feeding everything.

### Two-layer fallback hierarchy

**Layer 1 — factual data (dates, price band, lot size, issue size).**
`fetcher.py` walks providers in priority order:

1. **Groww** (primary, keyless) — `groww.in/ipo` is server-rendered (Next.js);
   the full IPO list is embedded as clean JSON in the page (`__NEXT_DATA__`),
   no headless browser needed. Tracks companies from the **DRHP filing stage**,
   so it surfaces IPOs days before NSE/BSE's own feeds do.
2. **IPOAlerts** (fallback, when `FALLBACK_API_KEY` is set) — aggregates NSE + BSE
   in one paginated API, and already includes GMP on every row
3. **NSE + BSE, aggregated** (last resort) — both queried and merged by company
   name (not just "try NSE, touch BSE only if NSE totally failed"); each fills
   gaps the other left blank (e.g. a price band one has and the other doesn't)

**Layer 2 — GMP (grey-market premium).** There is *no* official source for GMP.
This step only runs when layer 1 didn't already supply it. `gmp.py` walks:

1. **IPOWatch** (primary, keyless) — `ipowatch.in`'s live GMP table, plain
   server-rendered HTML (no JS rendering, no key, no rate limit seen so far)
2. **Aggregator API** (fallback) — IPOGuru (`PRIMARY_API_KEY`) or IPOAlerts
   (`FALLBACK_API_KEY`) — in practice this rarely has data for the specific
   IPOs this project fetches; IPOWatch is dedicated to tracking GMP and covers
   them far more reliably

GMP is then matched onto each IPO by normalized company name (`matching.py`). If
every layer-1 source fails, the system posts a red *Critical Error* embed and
retries next run. If GMP fails entirely, alerts still go out — just without the
grey-market number (it's enrichment, never a hard dependency).

> **Heads-up on the exchange endpoints:** NSE/BSE expose *undocumented internal*
> JSON APIs. They need browser headers + (for NSE) a cookie handshake — both are
> handled in `fetcher.py` — and they **block datacenter IPs**, so this runs
> reliably from a home connection but may be blocked on a cloud host. The BSE
> field mapping is best-effort; sanity-check it against a live response.
>
> **InvestorGain scraping was removed** (it was the original primary source):
> their site now renders its GMP table entirely client-side via JavaScript, so
> a plain HTTP GET returns an empty "No data available" shell with nothing left
> to parse. Groww is the new primary source as a result — and turned out to be
> a better one (broader coverage, earlier signal, no key, no rate limit seen so
> far). Same caveat as any unofficial scrape though: it can break if Groww
> restructures their page.

## Setup

### 1. Install
```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Get your keys (dual API setup)
| Variable | Where to get it |
|---|---|
| Discord (pick one) | **Bot:** `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` (invite the bot with *Send Messages* + *Embed Links*). **Webhook:** a real `DISCORD_WEBHOOK_URL` (`.../api/webhooks/<id>/<token>` — *not* a `channels/...` link) |
| `AI_API_KEY` | [OpenAI](https://platform.openai.com/api-keys) **or** [Google AI Studio](https://aistudio.google.com/apikey) — **required** |
| `PRIMARY_API_KEY` | *(optional)* [IPOGuru](https://www.ipoguru.in/ipo-gmp-details-developer-api) — GMP aggregator fallback only |
| `FALLBACK_API_KEY` | *(optional)* [IPOAlerts](https://ipoalerts.in/signup) — **primary facts source** (falls back to NSE/BSE if blank) |

**Only two things are truly required: a Discord delivery method (bot *or*
webhook) and `AI_API_KEY`.** Factual IPO data falls back to NSE/BSE with no key
— but `FALLBACK_API_KEY` (IPOAlerts) is recommended, since it's the primary
facts source and covers far more IPOs than NSE/BSE alone.

### 3. Configure
```bash
cp .env.example .env      # then edit .env and paste your keys
```

### 4. Run
```bash
# Dry run — fetch live NSE facts + GMP and PRINT them. No AI, no Discord, no keys.
python main.py --dry-run

# Demo — send 3 sample embeds to Discord. Needs only Discord creds (no AI/IPO keys).
python main.py --demo

# One-off real run (fetch, analyze, notify, then exit):
python main.py --once

# Long-running scheduler (daily at SCHEDULE_HOUR:SCHEDULE_MINUTE):
python main.py
```

Recommended first-time order: `--dry-run` (confirms live data) → `--demo`
(confirms Discord delivery) → `--once` (the real thing).

**Signals captured per IPO:** open/close dates, price band, lot size, issue size,
**subscription level** (× times subscribed, from NSE) and **GMP** (grey-market
premium, scraped) — all fed to the AI analyzer and shown in the embed.

## Configuration reference

All optional vars have defaults (see `.env.example`):

- `AI_PROVIDER` — `openai` (default) or `gemini`
- `AI_MODEL` — override the model id
- `LOOKAHEAD_DAYS` — scan window, default `21`
- `SCHEDULE_HOUR` / `SCHEDULE_MINUTE` — daily run time, default `08:00`
- `TIMEZONE` — IANA name, e.g. `Asia/Kolkata`, default `UTC`
- `DB_PATH` — SQLite file, default `state.db`
- `RUN_ON_START` — run once on boot, default `true`

## Discord embed colours

| AI risk level | Colour | Meaning |
|---|---|---|
| `LOW` | 🟢 Green | High-potential / lower risk |
| `MEDIUM` | 🟡 Yellow | Neutral |
| `HIGH` | 🔴 Red | High-risk |

## Deploying as a daemon

Because `main.py` runs its own APScheduler loop, you can keep it alive with any
process manager (systemd, `pm2`, Docker `restart: always`, or Windows Task
Scheduler running `python main.py --once` on a trigger). The built-in scheduler
is the simplest: just keep `python main.py` running.

## Notes & extending

- **State**: de-dup key is `symbol|expected_date|status`, so an IPO that moves
  from *expected* → *priced* correctly triggers a fresh alert.
- **Resilience**: a single bad IPO or a failed AI call won't kill the run — the
  analyzer degrades to a clearly-labelled fallback analysis.
- **Add a provider**: subclass `IPOProvider` in `fetcher.py`, implement `fetch()`
  to return `list[IPO]`, and add it to `IPOFetcher.providers` in priority order.
