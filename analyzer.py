"""
analyzer.py
===========
AI analysis engine.

Takes a normalized `IPO` record and produces a short, sharp analysis covering:
    * Market Sentiment
    * Potential Risks
    * A "Hype vs. Fundamentals" read (with a 0–100 hype score)
    * An overall risk level (LOW / MEDIUM / HIGH) used to colour the Discord embed

Supports two providers via `AI_PROVIDER`:
    * "openai"  -> uses the `openai` SDK (default)
    * "gemini"  -> uses the `google-genai` SDK

The analyzer is defensive: if the AI call fails or returns malformed JSON, it
degrades to a neutral, clearly-labelled fallback analysis rather than crashing
the pipeline.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from fetcher import IPO

log = logging.getLogger(__name__)

_VALID_RISK = {"LOW", "MEDIUM", "HIGH"}

_SYSTEM_PROMPT = (
    "You are a sharp, skeptical equity research analyst specializing in IPOs. "
    "You write concise, punchy assessments and you are not swayed by marketing "
    "hype. You always respond with STRICT JSON matching the requested schema — "
    "no markdown, no prose outside the JSON."
)

_USER_TEMPLATE = """\
Analyze the following upcoming IPO and respond with STRICT JSON only.

Company: {name}
{overview}

Return JSON with EXACTLY these keys:
{{
  "market_sentiment": "<1-2 sentence read on how the market is likely to receive this>",
  "potential_risks": ["<risk 1>", "<risk 2>", "<risk 3>"],
  "hype_vs_fundamentals": "<one short verdict, e.g. 'Story-driven, thin fundamentals'>",
  "hype_score": <integer 0-100, where 0 = pure fundamentals and 100 = pure hype>,
  "risk_level": "<LOW | MEDIUM | HIGH>",
  "summary": "<2-3 sentence overall take an investor could act on>"
}}

Base your judgement on the data provided. If data is sparse, say so and lean
toward a more cautious risk_level. Do not invent specific financial figures.
"""

_BATCH_TEMPLATE = """\
Analyze the following {count} IPOs and respond with STRICT JSON only.

IPOs (index. name | facts):
{listing}

Return a JSON object of the form:
{{"analyses": [
  {{
    "market_sentiment": "<1 sentence>",
    "potential_risks": ["<risk 1>", "<risk 2>"],
    "hype_vs_fundamentals": "<one short verdict>",
    "hype_score": <integer 0-100>,
    "risk_level": "<LOW | MEDIUM | HIGH>",
    "summary": "<1-2 sentence take>"
  }}
]}}

The "analyses" array MUST have exactly {count} items, in the SAME ORDER as the
IPOs above. Keep each field concise. If data is sparse, lean cautious. Do not
invent specific financial figures.
"""


@dataclass
class Analysis:
    market_sentiment: str
    potential_risks: list[str]
    hype_vs_fundamentals: str
    hype_score: int
    risk_level: str
    summary: str
    model: str
    is_fallback: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class Analyzer:
    def __init__(self, provider: str, api_key: str, model: str) -> None:
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self._client = self._build_client()

    def _build_client(self) -> Any:
        if self.provider == "openai":
            from openai import OpenAI  # imported lazily so the other SDK is optional

            return OpenAI(api_key=self.api_key)
        if self.provider == "gemini":
            from google import genai

            return genai.Client(api_key=self.api_key)
        if self.provider == "claude":
            return None  # uses the `claude -p` CLI via subprocess; no client object
        raise ValueError(f"Unsupported AI provider: {self.provider!r}")

    # ------------------------------------------------------------------ #
    def analyze(self, ipo: IPO, *, retries: int = 3) -> Analysis:
        prompt = _USER_TEMPLATE.format(name=ipo.name, overview=ipo.financial_overview)
        label = ipo.symbol or ipo.name
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                data = self._parse(self._complete(prompt))
                return self._to_analysis(data, fallback=False)
            except Exception as exc:  # noqa: BLE001 - never let analysis crash the run
                last_exc = exc
                log.warning("AI attempt %d/%d failed for %s: %r", attempt, retries, label, exc)
                if attempt < retries:
                    time.sleep(1.5 * attempt)  # simple linear backoff for transient 5xx
        log.warning("AI analysis failed after %d attempts for %s: %r", retries, label, last_exc)
        return self._fallback(ipo)

    def analyze_batch(self, ipos: list[IPO], *, retries: int = 2) -> list[Analysis]:
        """Analyze many IPOs in a SINGLE model call (fast/cheap for digests).

        Returns a list of Analysis aligned 1:1 with `ipos`. Falls back per-IPO if
        the batch call fails or returns the wrong number of items.
        """
        if not ipos:
            return []
        listing = "\n".join(
            f"{i}. {ipo.name} | {ipo.financial_overview}" for i, ipo in enumerate(ipos, 1)
        )
        prompt = _BATCH_TEMPLATE.format(count=len(ipos), listing=listing)
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                data = self._parse(self._complete(prompt))
                arr = data.get("analyses") if isinstance(data, dict) else data
                if not isinstance(arr, list) or not arr:
                    raise ValueError("batch response missing 'analyses' array")
                out: list[Analysis] = []
                for i, ipo in enumerate(ipos):
                    item = arr[i] if i < len(arr) else {}
                    out.append(
                        self._to_analysis(item, fallback=False) if item else self._fallback(ipo)
                    )
                return out
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                log.warning("Batch AI attempt %d/%d failed: %r", attempt, retries, exc)
                if attempt < retries:
                    time.sleep(1.5 * attempt)
        log.warning("Batch analysis failed after %d attempts: %r", retries, last_exc)
        return [self._fallback(ipo) for ipo in ipos]

    # ------------------------------------------------------------------ #
    def _complete(self, prompt: str) -> str:
        if self.provider == "openai":
            resp = self._client.chat.completions.create(
                model=self.model,
                temperature=0.4,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
            return resp.choices[0].message.content or ""

        if self.provider == "claude":
            return self._complete_claude_cli(prompt)

        # gemini
        resp = self._client.models.generate_content(
            model=self.model,
            contents=f"{_SYSTEM_PROMPT}\n\n{prompt}",
            config={"response_mime_type": "application/json", "temperature": 0.4},
        )
        return resp.text or ""

    def _complete_claude_cli(self, prompt: str) -> str:
        """Run the local `claude -p` CLI in headless JSON mode and return text.

        The prompt is passed via STDIN (an argv arg gets mangled by the Windows
        npm .cmd shim). Tools are disabled and a strict system prompt is appended
        so the coding agent behaves as a pure JSON responder (no web search, no
        follow-up questions).
        """
        import os
        import shutil
        import subprocess
        import tempfile

        exe = shutil.which("claude") or "claude"
        system = (
            _SYSTEM_PROMPT + " Use ONLY the data in the user message. NEVER use "
            "tools or web search. NEVER ask questions. Output ONLY the JSON object."
        )
        cmd = [
            exe, "-p", "--output-format", "json",
            "--allowedTools", "",             # disable all tools (no web search)
            "--append-system-prompt", system,
        ]
        if self.model:
            cmd += ["--model", self.model]

        # Pass the prompt via a real file on stdin. Piping large input with
        # `input=` crashes the Windows npm .cmd shim (node exit 0xC0000409),
        # whereas a redirected file handle works reliably.
        tmp = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".txt", delete=False
        )
        try:
            tmp.write(prompt)
            tmp.close()
            with open(tmp.name, "r", encoding="utf-8") as stdin_fh:
                proc = subprocess.run(
                    cmd, stdin=stdin_fh, capture_output=True, text=True,
                    encoding="utf-8", timeout=180,
                )
        finally:
            os.unlink(tmp.name)
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude CLI failed (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '')[:300]}"
            )
        out = (proc.stdout or "").strip()
        try:
            env = json.loads(out)  # {type:result, result:"...", is_error:bool, ...}
        except json.JSONDecodeError:
            return out  # already raw text
        if env.get("is_error"):
            raise RuntimeError(f"claude CLI error: {str(env.get('result'))[:300]}")
        return env.get("result", out)

    @staticmethod
    def _parse(raw_text: str) -> dict[str, Any]:
        text = raw_text.strip()
        # Strip accidental markdown code fences if the model added them.
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        return json.loads(text)

    def _to_analysis(self, data: dict[str, Any], *, fallback: bool) -> Analysis:
        risk = str(data.get("risk_level", "MEDIUM")).strip().upper()
        if risk not in _VALID_RISK:
            risk = "MEDIUM"

        try:
            hype = int(data.get("hype_score", 50))
        except (TypeError, ValueError):
            hype = 50
        hype = max(0, min(100, hype))

        risks = data.get("potential_risks") or []
        if isinstance(risks, str):
            risks = [risks]
        risks = [str(r).strip() for r in risks if str(r).strip()][:5]

        return Analysis(
            market_sentiment=str(data.get("market_sentiment", "No sentiment available.")).strip(),
            potential_risks=risks or ["No specific risks identified."],
            hype_vs_fundamentals=str(data.get("hype_vs_fundamentals", "Unclear")).strip(),
            hype_score=hype,
            risk_level=risk,
            summary=str(data.get("summary", "No summary available.")).strip(),
            model=self.model,
            is_fallback=fallback,
            raw=data,
        )

    def _fallback(self, ipo: IPO) -> Analysis:
        return Analysis(
            market_sentiment="AI analysis unavailable — showing raw IPO data only.",
            potential_risks=[
                "Automated analysis could not be generated for this IPO.",
                "Do your own due diligence before acting.",
            ],
            hype_vs_fundamentals="Not assessed",
            hype_score=50,
            risk_level="MEDIUM",
            summary=(
                f"{ipo.name} ({ipo.symbol or 'N/A'}) is scheduled around "
                f"{ipo.expected_date or 'an unknown date'} at {ipo.price_range}. "
                "AI analysis was unavailable at run time."
            ),
            model=self.model,
            is_fallback=True,
        )
