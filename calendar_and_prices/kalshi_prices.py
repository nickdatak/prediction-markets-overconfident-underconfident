"""Kalshi quotes and last trade at each contest's snapshot, saved to kalshi_prices.csv.

    python3 kalshi_prices.py

For each market of every Kalshi contest in contest_calendar.csv:
  quote   best YES bid and ask at the snapshot: the close of the last candle ending at or before
          it. Kalshi only writes a candle when something changes, so a quiet market is looked for
          in 1-minute candles over the 6 hours before, then hourly over 30 days, then daily over a year.
  trade   the last trade at or before the snapshot, looked for in the hour before, then the day before.
Every API response is kept in raw/kalshi_responses.jsonl.gz. Standard library only.
"""

import csv
import gzip
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARKETS = HERE.parent / "cleaning" / "kalshi_binary_elections.json"
CALENDAR = HERE / "contest_calendar.csv"
OUT = HERE / "kalshi_prices.csv"
RAW = HERE / "raw" / "kalshi_responses.jsonl.gz"

BASE = "https://api.elections.kalshi.com/trade-api/v2"
HEADERS = {  # Kalshi rejects requests that do not look like a browser
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json", "Referer": "https://kalshi.com/", "Origin": "https://kalshi.com",
}
CANDLE_SEARCH = [(1, timedelta(hours=6)), (60, timedelta(days=30)), (1440, timedelta(days=365))]
TRADE_SEARCH = [(timedelta(0), timedelta(hours=1)), (timedelta(hours=1), timedelta(days=1))]
FIELDS = ["contest_id", "ticker", "candidate", "result", "snapshot_utc", "quote_utc", "quote_age_minutes",
          "yes_bid", "yes_ask", "candle_minutes", "endpoint", "last_trade_utc", "last_trade_price",
          "minutes_since_trade", "error"]

RAW_LOCK = threading.Lock()
raw_log = None


class NotFound(Exception):
    pass


def get(path, params):
    url = f"{BASE}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    for attempt in range(7):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=60) as response:
                body = json.loads(response.read())
            with RAW_LOCK:
                raw_log.write(json.dumps({"url": url, "received_utc": datetime.now(timezone.utc).isoformat(),
                                          "body": body}) + "\n")
            return body
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404):
                raise NotFound(url) from None
            if exc.code not in (403, 408, 429, 500, 502, 503, 504) or attempt == 6:
                raise
        except (OSError, ValueError):  # timeouts, dropped connections, cut-off responses
            if attempt == 6:
                raise
        time.sleep(min(60, 2 ** attempt))


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def dollars(node):
    """A candle price in dollars. The live endpoint gives close_dollars; the historical one gives
    close as a dollar string ("0.0800"); old responses gave close as a whole number of cents."""
    if not isinstance(node, dict):
        return None
    if node.get("close_dollars") not in (None, ""):
        return number(node["close_dollars"])
    close = node.get("close")
    if close in (None, ""):
        return None
    if isinstance(close, int) or (isinstance(close, str) and "." not in close):
        return number(close) / 100.0 if number(close) is not None else None
    return number(close)


def trade_price(trade):
    if trade.get("yes_price_dollars") not in (None, ""):
        return number(trade["yes_price_dollars"])
    cents = number(trade.get("yes_price"))
    return None if cents is None else cents / 100.0


def stamp(text):
    """ISO time to an aware datetime; pads short fractions of a second, which Python 3.10 rejects."""
    text = text.strip().replace("Z", "+00:00")
    text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text)
    return datetime.fromisoformat(text)


def unix(moment):
    return int(moment.timestamp())


def utc_text(moment, seconds=False):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ" if seconds else "%Y-%m-%dT%H:%MZ")


def quote_at(market, snapshot, historical_first):
    live = f"/series/{market['series_ticker']}/markets/{market['ticker']}/candlesticks"
    historical = f"/historical/markets/{market['ticker']}/candlesticks"
    paths = [historical, live] if historical_first else [live, historical]
    for minutes, lookback in CANDLE_SEARCH:
        params = {"period_interval": minutes, "start_ts": unix(snapshot - lookback), "end_ts": unix(snapshot)}
        for path in paths:
            try:
                body = get(path, params)
            except NotFound:
                continue
            candles = []
            for candle in body.get("candlesticks") or []:
                end = candle.get("end_period_ts")
                bid, ask = dollars(candle.get("yes_bid")), dollars(candle.get("yes_ask"))
                if end is not None and int(end) <= unix(snapshot) and (bid is not None or ask is not None):
                    candles.append((int(end), bid, ask))
            if candles:
                end, bid, ask = max(candles)
                return {"quote_utc": datetime.fromtimestamp(end, timezone.utc), "yes_bid": bid, "yes_ask": ask,
                        "candle_minutes": minutes, "endpoint": "historical" if path == historical else "live"}
    return None


def last_trade(ticker, snapshot, historical_first):
    paths = ["/historical/trades", "/markets/trades"]
    if not historical_first:
        paths.reverse()
    for near, far in TRADE_SEARCH:
        for path in paths:
            trades, cursor = [], None
            try:
                while True:
                    params = {"ticker": ticker, "min_ts": unix(snapshot - far), "max_ts": unix(snapshot - near),
                              "limit": 1000}
                    if cursor:
                        params["cursor"] = cursor
                    body = get(path, params)
                    trades += body.get("trades") or []
                    cursor = body.get("cursor")
                    if not cursor or not body.get("trades"):
                        break
            except NotFound:
                continue
            trades = [(stamp(t["created_time"]), trade_price(t)) for t in trades if t.get("created_time")]
            trades = [t for t in trades if t[0] <= snapshot]
            if trades:
                return max(trades, key=lambda t: t[0])
    return None


def price_row(market, snapshot, cutoff):
    row = {"contest_id": market["event_ticker"], "ticker": market["ticker"], "candidate": market.get("yes_sub_title"),
           "result": market.get("result"), "snapshot_utc": utc_text(snapshot), "error": ""}
    try:
        quote = quote_at(market, snapshot, stamp(market["settlement_ts"]) < cutoff["markets"])
        trade = last_trade(market["ticker"], snapshot, snapshot < cutoff["trades"])
    except Exception as exc:  # keep going; the row records the error
        row["error"] = repr(exc)[:300]
        return row
    if quote:
        row.update(quote, quote_utc=utc_text(quote["quote_utc"]),
                   quote_age_minutes=round((snapshot - quote["quote_utc"]).total_seconds() / 60, 1))
    if trade:
        row.update(last_trade_utc=utc_text(trade[0], seconds=True), last_trade_price=trade[1],
                   minutes_since_trade=round((snapshot - trade[0]).total_seconds() / 60, 1))
    return row


def main():
    global raw_log
    with CALENDAR.open(newline="") as handle:
        snapshots = {r["contest_id"]: stamp(r["snapshot_utc"]) for r in csv.DictReader(handle) if r["platform"] == "Kalshi"}
    markets = [m for m in json.loads(MARKETS.read_text()) if m["event_ticker"] in snapshots]
    missing = set(snapshots) - {m["event_ticker"] for m in markets}
    if missing:
        raise SystemExit(f"{len(missing)} calendar contests are not in {MARKETS.name}, e.g. {sorted(missing)[:3]}")

    RAW.parent.mkdir(exist_ok=True)
    raw_log = gzip.open(RAW, "wt")
    try:
        try:  # markets settled, and trades made, before these times are only on the historical endpoints
            body = get("/historical/cutoff", None)
            cutoff = {"markets": stamp(body["market_settled_ts"]), "trades": stamp(body["trades_created_ts"])}
        except Exception:  # not essential: every lookup falls back to the other endpoint
            cutoff = {"markets": datetime.now(timezone.utc), "trades": datetime.now(timezone.utc)}
        print(f"{len(markets)} markets in {len(snapshots)} contests; "
              f"historical endpoints hold markets settled before {cutoff['markets']:%Y-%m-%d}")
        rows = []
        with ThreadPoolExecutor(4) as pool:
            jobs = pool.map(lambda m: price_row(m, snapshots[m["event_ticker"]], cutoff), markets)
            for i, row in enumerate(jobs, 1):
                rows.append(row)
                if i % 100 == 0 or i == len(markets):
                    print(f"  {i}/{len(markets)}", flush=True)
    finally:
        raw_log.close()

    rows.sort(key=lambda r: (r["contest_id"], r["ticker"]))
    with OUT.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    errors = sum(bool(r["error"]) for r in rows)
    no_quote = sum(not r["error"] and r.get("yes_bid") is None and r.get("yes_ask") is None for r in rows)
    no_trade = sum(not r["error"] and not r.get("last_trade_utc") for r in rows)
    print(f"{len(rows)} markets -> {OUT.name}: {no_quote} without a quote, {no_trade} without a trade "
          f"in the day before, {errors} errors" + (" (run again)" if errors else ""))


if __name__ == "__main__":
    main()
