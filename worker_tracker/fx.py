"""Currency conversion for the payroll digest. Used to display a single
USD-equivalent total even when workers are paid in mixed currencies.

Strategy:
  1. Fetch live rates from open.er-api.com (free, no API key needed).
  2. Cache for 24h to avoid hammering the API.
  3. Fall back to a hardcoded approximate table if the API is unreachable
     — payroll still ships, the total is just slightly stale.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

# Approximate rates as of mid-2026 — used only if live fetch fails.
# Format: 1 USD = X local currency.
_FALLBACK_RATES = {
    "USD": 1.0,
    "CAD": 1.37,
    "PHP": 58.5,
    "EUR": 0.92,
    "GBP": 0.79,
    "AUD": 1.51,
    "MXN": 18.0,
    "INR": 83.0,
    "JPY": 150.0,
    "BRL": 5.1,
}

_cache: dict[str, float] = {}
_cache_ts: float = 0.0
_CACHE_TTL_SECONDS = 24 * 3600


def _fetch_live_rates() -> Optional[dict[str, float]]:
    try:
        url = "https://open.er-api.com/v6/latest/USD"
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
        if data.get("result") == "success" and isinstance(data.get("rates"), dict):
            rates = {k.upper(): float(v) for k, v in data["rates"].items()}
            log.info("Fetched live FX rates (%d currencies)", len(rates))
            return rates
        log.warning("FX API returned no rates")
    except Exception as e:
        log.warning("FX live fetch failed: %s — using fallback", e)
    return None


def get_rates() -> dict[str, float]:
    """Return current rates {currency: rate per USD}. Cached 24h."""
    global _cache, _cache_ts
    now = time.time()
    if _cache and (now - _cache_ts) < _CACHE_TTL_SECONDS:
        return _cache
    live = _fetch_live_rates()
    rates = live or dict(_FALLBACK_RATES)
    _cache = rates
    _cache_ts = now
    return rates


def to_usd(amount: float, currency: str, rates: dict[str, float] | None = None) -> float:
    """Convert `amount` in `currency` to USD."""
    currency = (currency or "USD").upper()
    if currency == "USD":
        return float(amount)
    if rates is None:
        rates = get_rates()
    rate = rates.get(currency)
    if not rate:
        log.warning("No FX rate for %s — treating as 0 USD", currency)
        return 0.0
    return float(amount) / rate
