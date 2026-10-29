"""Every settled market in Kalshi's Elections category, saved to data/kalshi_elections.json.

    python3 kalshi_elections.py
    python3 kalshi_elections.py --open    # markets still open instead, to data/kalshi_elections_open.json
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = "https://api.elections.kalshi.com/trade-api/v2"
HEADERS = {  # Kalshi rejects requests that do not look like a browser
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://kalshi.com/",
    "Origin": "https://kalshi.com",
}
OPEN = "--open" in sys.argv[1:]
OUT = Path(__file__).resolve().parent / "data" / ("kalshi_elections_open.json" if OPEN else "kalshi_elections.json")


class NotFound(Exception):
    pass


def get(path, params):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(7):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=60) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404):
                raise NotFound(url) from exc
            if exc.code not in (403, 408, 429, 500, 502, 503, 504) or attempt == 6:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 6:
                raise
        time.sleep(2 ** attempt)


def pages(path, params, key):
    items, cursor = [], None
    while True:
        body = get(path, dict(params, cursor=cursor) if cursor else params)
        items += body.get(key) or []
        cursor = body.get("cursor")
        if not cursor:
            return items


def series_markets(series_ticker):
    # Older markets are only on the historical endpoint, newer ones only on the live one.
    markets = {}
    queries = [("/markets", {"series_ticker": series_ticker, "status": "open", "limit": 1000})] if OPEN else [
        ("/historical/markets", {"series_ticker": series_ticker, "limit": 200}),
        ("/markets", {"series_ticker": series_ticker, "status": "settled", "limit": 1000}),
    ]
    for path, params in queries:
        try:
            for market in pages(path, params, "markets"):
                markets.setdefault(market["ticker"], dict(market, series_ticker=series_ticker))
        except NotFound:
            pass
    return list(markets.values())


def safe(series_ticker):
    try:
        return series_markets(series_ticker), None
    except Exception as exc:
        return [], f"{series_ticker}: {exc}"


if __name__ == "__main__":
    tickers = [s["ticker"] for s in pages("/series", {"category": "Elections"}, "series")]
    print(f"{len(tickers)} series in Elections")

    markets, failed = [], []
    with ThreadPoolExecutor(4) as pool:
        for i, (found, error) in enumerate(pool.map(safe, tickers), 1):
            markets += found
            if error:
                failed.append(error)
            if i % 100 == 0:
                print(f"  {i}/{len(tickers)} series, {len(markets)} markets")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(markets))
    print(f"{len(markets)} {'open' if OPEN else 'settled'} markets -> {OUT}")
    if failed:
        print(f"{len(failed)} series failed, run again:", *failed, sep="\n  ")
