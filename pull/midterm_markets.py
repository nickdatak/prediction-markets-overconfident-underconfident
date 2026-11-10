"""The current state of every contest on the midterm list, for the run after the vote.

    python3 pull/midterm_markets.py

Reads  ../calendar_and_prices/midterm_contests.csv   the list made before the vote
Writes data/midterms_kalshi.json       every market of each listed Kalshi event
       data/midterms_polymarket.json   each listed Polymarket event with its markets
The frozen training pulls in data/ are not touched. Standard library only.
"""

import csv
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIST = HERE.parent / "calendar_and_prices" / "midterm_contests.csv"
OUT_KALSHI = HERE / "data" / "midterms_kalshi.json"
OUT_POLYMARKET = HERE / "data" / "midterms_polymarket.json"
OPEN_KALSHI = HERE / "data" / "kalshi_elections_open.json"  # the pull the list was made from; has series tickers

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json", "Referer": "https://kalshi.com/", "Origin": "https://kalshi.com",
}
GAP = {"kalshi": 0.15, "gamma": 0.05}  # seconds between requests to each API

PACE_LOCK = threading.Lock()
next_slot, paused_until = {}, {}


def wait_turn(gap, key="all"):
    """Space requests from all threads at least `gap` seconds apart: the APIs limit the request rate."""
    while True:
        with PACE_LOCK:
            now = time.monotonic()
            slot = max(now, next_slot.get(key, 0.0), paused_until.get(key, 0.0))
            next_slot[key] = slot + gap
        time.sleep(max(0.0, slot - now))
        with PACE_LOCK:  # a rate-limit answer may have come in while this thread slept
            if paused_until.get(key, 0.0) <= time.monotonic():
                return


def retry_wait(exc, attempt):
    """How long to wait before the next try: the server's Retry-After if it gave one, else doubling to a minute."""
    backoff = min(60, 2 ** attempt)
    try:
        return min(120.0, max(backoff, float(exc.headers.get("Retry-After"))))
    except (AttributeError, TypeError, ValueError):
        return backoff


def pause_all(seconds, key="all"):
    """After a rate-limit answer, hold every thread back, not only the one that got it."""
    with PACE_LOCK:
        paused_until[key] = max(paused_until.get(key, 0.0), time.monotonic() + seconds)


SERIES = {}  # event ticker -> series ticker, from the open pull


def get(url, params=None):
    host = "kalshi" if url.startswith(KALSHI) else "gamma"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    for attempt in range(10):
        wait_turn(GAP[host], host)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=60) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code not in (403, 408, 425, 429, 500, 502, 503, 504) or attempt == 9:
                raise
            if exc.code == 429:
                pause_all(retry_wait(exc, attempt), host)  # the next wait_turn waits it out
            else:
                time.sleep(retry_wait(exc, attempt))
        except (OSError, ValueError):  # timeouts, dropped connections, cut-off responses
            if attempt == 9:
                raise
            time.sleep(min(60, 2 ** attempt))


def kalshi_event(ticker):
    markets, cursor = [], None
    while True:
        params = {"event_ticker": ticker, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        body = get(f"{KALSHI}/markets", params) or {}
        markets += body.get("markets") or []
        cursor = body.get("cursor")
        if not cursor or not body.get("markets"):
            break
    if not markets:  # settled long enough ago to have moved to the historical endpoint
        body = get(f"{KALSHI}/historical/markets", {"event_ticker": ticker, "limit": 1000}) or {}
        markets = body.get("markets") or []
    series = SERIES.get(ticker)  # the price pull needs it for the candle path
    if not series:
        event = (get(f"{KALSHI}/events/{ticker}") or {}).get("event") or {}
        series = event.get("series_ticker") or ticker.split("-")[0]
    return [dict(m, series_ticker=m.get("series_ticker") or series) for m in markets]


def polymarket_event(event_id):
    return get(f"{GAMMA}/events/{event_id}")


def main():
    with LIST.open(newline="") as handle:
        listed = list(csv.DictReader(handle))
    kalshi_ids = [r["contest_id"] for r in listed if r["platform"] == "Kalshi"]
    poly_ids = [r["contest_id"] for r in listed if r["platform"] == "Polymarket"]
    print(f"{len(kalshi_ids)} Kalshi and {len(poly_ids)} Polymarket contests on the list")
    if OPEN_KALSHI.exists():
        SERIES.update((m["event_ticker"], m["series_ticker"]) for m in json.loads(OPEN_KALSHI.read_text())
                      if m.get("series_ticker"))

    with ThreadPoolExecutor(4) as pool:
        kalshi = [m for markets in pool.map(kalshi_event, kalshi_ids) for m in markets]
        OUT_KALSHI.write_text(json.dumps(kalshi))
        print(f"  Kalshi: {len(kalshi)} markets in {len({m['event_ticker'] for m in kalshi})} events", flush=True)
        events = [e for e in pool.map(polymarket_event, poly_ids) if e]
        OUT_POLYMARKET.write_text(json.dumps(events))
        print(f"  Polymarket: {len(events)} events")

    missing = (len(kalshi_ids) - len({m["event_ticker"] for m in kalshi})) + (len(poly_ids) - len(events))
    print(f"-> {OUT_KALSHI.name}, {OUT_POLYMARKET.name}" + (f"; {missing} listed contests not found" if missing else ""))


if __name__ == "__main__":
    main()
