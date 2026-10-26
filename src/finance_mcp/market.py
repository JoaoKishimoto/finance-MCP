"""Market data: B3 quotes (brapi.dev) and Tesouro Direto rates (Tesouro Transparente open data).

Both are cached so repeated questions don't hit the sources again. The Tesouro file
(~15 MB CSV with the full history) is streamed line by line and only the latest day
is kept in memory, so RAM use stays tiny.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime
from typing import Any

import httpx

BRAPI_URL = "https://brapi.dev/api/quote/{ticker}"
TESOURO_CSV_URL = (
    "https://www.tesourotransparente.gov.br/ckan/dataset/df56aa42-484a-4a59-8184-7676580c81e3"
    "/resource/796d2059-14e9-44e3-80c9-2d9e30b405c1/download/PrecoTaxaTesouroDireto.csv"
)
QUOTE_TTL = 15 * 60
TESOURO_TTL = 6 * 60 * 60


def _br_number(text: str) -> float | None:
    text = text.strip()
    if not text:
        return None
    return float(text.replace(".", "").replace(",", "."))


def _br_date(text: str) -> date:
    return datetime.strptime(text.strip(), "%d/%m/%Y").date()


class MarketData:
    def __init__(self, brapi_token: str = ""):
        self._brapi_token = brapi_token
        self._http = httpx.AsyncClient(timeout=60, follow_redirects=True)
        self._quotes: dict[str, tuple[float, dict[str, Any]]] = {}
        self._tesouro: tuple[float, dict[str, Any]] | None = None
        self._tesouro_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _quote(self, ticker: str) -> dict[str, Any]:
        ticker = ticker.strip().upper()
        hit = self._quotes.get(ticker)
        if hit and time.monotonic() - hit[0] < QUOTE_TTL:
            return hit[1]
        # Token goes in a header, never in the URL (URLs end up in logs).
        headers = {"Authorization": f"Bearer {self._brapi_token}"} if self._brapi_token else {}
        for attempt in range(3):
            resp = await self._http.get(BRAPI_URL.format(ticker=ticker), headers=headers)
            if resp.status_code != 429:
                break
            await asyncio.sleep(1 + attempt)  # rate limited: wait and retry
        if resp.status_code >= 400:
            return {"symbol": ticker, "error": f"brapi returned {resp.status_code}: {resp.text[:200]}"}
        raw = (resp.json().get("results") or [{}])[0]
        quote = {
            "symbol": raw.get("symbol", ticker),
            "name": raw.get("longName") or raw.get("shortName"),
            "price": raw.get("regularMarketPrice"),
            "change_pct_today": raw.get("regularMarketChangePercent"),
            "day_range": raw.get("regularMarketDayRange"),
            "52w_range": raw.get("fiftyTwoWeekRange"),
            "market_cap": raw.get("marketCap"),
            "price_earnings": raw.get("priceEarnings"),
            "earnings_per_share": raw.get("earningsPerShare"),
            "currency": raw.get("currency"),
            "market_time": raw.get("regularMarketTime"),
        }
        self._quotes[ticker] = (time.monotonic(), quote)
        return quote

    async def quotes(self, tickers: list[str]) -> list[dict[str, Any]]:
        # brapi's free plan allows one ticker per request and one request at a time.
        sem = asyncio.Semaphore(1)

        async def one(t: str) -> dict[str, Any]:
            async with sem:
                return await self._quote(t)

        return await asyncio.gather(*(one(t) for t in tickers if t.strip()))

    async def tesouro_rates(self) -> dict[str, Any]:
        async with self._tesouro_lock:
            if self._tesouro and time.monotonic() - self._tesouro[0] < TESOURO_TTL:
                return self._tesouro[1]
            latest: date | None = None
            rows: list[dict[str, Any]] = []
            async with self._http.stream("GET", TESOURO_CSV_URL) as resp:
                resp.raise_for_status()
                header_skipped = False
                async for line in resp.aiter_lines():
                    if not header_skipped:
                        header_skipped = True
                        continue
                    parts = line.split(";")
                    if len(parts) < 8:
                        continue
                    base = _br_date(parts[2])
                    if latest is not None and base < latest:
                        continue
                    if latest is None or base > latest:
                        latest, rows = base, []
                    rows.append(
                        {
                            "bond": f"{parts[0]} {_br_date(parts[1]).year}",
                            "type": parts[0],
                            "maturity": _br_date(parts[1]).isoformat(),
                            "buy_rate_pct": _br_number(parts[3]),
                            "sell_rate_pct": _br_number(parts[4]),
                            "buy_price": _br_number(parts[5]),
                            "sell_price": _br_number(parts[6]),
                        }
                    )
            rows.sort(key=lambda r: (r["type"], r["maturity"]))
            result = {
                "reference_date": latest.isoformat() if latest else None,
                "source": "Tesouro Transparente (official open data, updated once per business day)",
                "note": "Rates are % per year. For IPCA+ and Renda+ bonds the rate is the real rate above IPCA; "
                "for Selic bonds it is the spread over Selic; for Prefixado it is the nominal rate.",
                "bonds": rows,
            }
            self._tesouro = (time.monotonic(), result)
            return result
