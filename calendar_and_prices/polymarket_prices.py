"""Polymarket prices and last trade at each contest's snapshot, saved to polymarket_prices.csv.

    python3 polymarket_prices.py

For each option of every Polymarket contest in contest_calendar.csv (the two candidates and, where
there is one, the "another candidate" basket):
  price   the last point of Polymarket's recorded price history at or before the snapshot, looked
          for at 1-minute spacing over the 6 hours before, then hourly over 2 days. The history has
          a point every minute even when nothing trades, so it says nothing about staleness.
  trade   (candidates only) the last trade in the candidate's market at or before the snapshot,
          looked for in the hour before, then the day before.
Every API response is kept in raw/polymarket_responses.jsonl.gz. Standard library only.
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
EVENTS = HERE.parent / "cleaning" / "polymarket_binary_elections.json"
CALENDAR = HERE / "contest_calendar.csv"
OUT = HERE / "polymarket_prices.csv"
RAW = HERE / "raw" / "polymarket_responses.jsonl.gz"

CLOB = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
           "Accept": "application/json"}
PRICE_SEARCH = [(1, timedelta(hours=6)), (60, timedelta(days=2))]
TRADE_SEARCH = [(timedelta(0), timedelta(hours=1)), (timedelta(hours=1), timedelta(days=1))]
TRADE_PAGE = 1000
FIELDS = ["contest_id", "market_id", "role", "candidate", "won", "snapshot_utc", "price_utc", "price_age_minutes",
          "price", "price_minutes", "last_trade_utc", "last_trade_price", "minutes_since_trade", "token_id",
          "condition_id", "error"]

RAW_LOCK = threading.Lock()
raw_log = None


class NotFound(Exception):
    pass


def get(url, params):
    url = f"{url}?{urllib.parse.urlencode(params)}"
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
            if exc.code not in (403, 408, 425, 429, 500, 502, 503, 504) or attempt == 6:
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


def as_list(value):
    return json.loads(value) if isinstance(value, str) else list(value or [])


def stamp(text):
    """ISO time to an aware datetime; pads short fractions of a second, which Python 3.10 rejects."""
    text = text.strip().replace("Z", "+00:00")
    text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text)
    return datetime.fromisoformat(text)


def epoch(value):
    seconds = int(float(value))
    return seconds // 1000 if seconds > 10_000_000_000 else seconds  # some responses are in milliseconds


def unix(moment):
    return int(moment.timestamp())


def utc_text(moment, seconds=False):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ" if seconds else "%Y-%m-%dT%H:%MZ")


def options(event):
    """(market_id, role, name, won, token_id, condition_id) for each option of a contest."""
    markets = event["markets"]
    if len(markets) == 1:  # one market whose two outcomes are the two candidates
        market = markets[0]
        names, tokens, prices = as_list(market["outcomes"]), as_list(market["clobTokenIds"]), as_list(market["outcomePrices"])
        return [(market["id"], "candidate", names[i], number(prices[i]) == 1, tokens[i], market["conditionId"])
                for i in range(2)]
    baskets = set(event.get("basket_market_ids") or [])
    rows = []
    for market in markets:  # one Yes/No market per option
        outcomes = [o.lower() for o in as_list(market["outcomes"])]
        yes = outcomes.index("yes")
        rows.append((market["id"], "basket" if market["id"] in baskets else "candidate",
                     market.get("groupItemTitle") or market.get("question"),
                     number(as_list(market["outcomePrices"])[yes]) == 1,
                     as_list(market["clobTokenIds"])[yes], market["conditionId"]))
    return rows


def price_at(token, snapshot):
    for minutes, lookback in PRICE_SEARCH:
        try:
            body = get(f"{CLOB}/prices-history", {"market": token, "startTs": unix(snapshot - lookback),
                                                  "endTs": unix(snapshot), "fidelity": minutes})
        except NotFound:
            continue
        points = [(epoch(p["t"]), number(p["p"])) for p in (body.get("history") or [])
                  if p.get("t") is not None and number(p.get("p")) is not None]
        points = [p for p in points if p[0] <= unix(snapshot)]
        if points:
            moment, price = max(points)
            return datetime.fromtimestamp(moment, timezone.utc), price, minutes
    return None


def trades_page(condition_id, start, end):
    return get(f"{DATA_API}/trades", {"market": condition_id, "start": unix(start), "end": unix(end),
                                      "limit": TRADE_PAGE, "offset": 0, "takerOnly": "true"})


def last_trade(condition_id, snapshot):
    for near, far in TRADE_SEARCH:
        try:
            trades = trades_page(condition_id, snapshot - far, snapshot - near)
        except NotFound:
            continue
        trades = [(epoch(t["timestamp"]), number(t.get("price"))) for t in (trades or []) if t.get("timestamp")]
        trades = [t for t in trades if t[0] <= unix(snapshot)]
        if trades:  # a full page still has a trade inside the window, which is all the rules need
            moment, price = max(trades)
            return datetime.fromtimestamp(moment, timezone.utc), price
    return None


def contest_rows(job):
    contest_id, snapshot, event = job
    rows, trades = [], {}  # both candidates of a one-market contest share a market: look it up once
    for market_id, role, name, won, token, condition in options(event):
        row = {"contest_id": contest_id, "market_id": market_id, "role": role, "candidate": name,
               "won": "true" if won else "false", "snapshot_utc": utc_text(snapshot), "token_id": token,
               "condition_id": condition, "error": ""}
        rows.append(row)
        try:
            price = price_at(token, snapshot)
            if role == "candidate" and condition not in trades:
                trades[condition] = last_trade(condition, snapshot)
        except Exception as exc:  # keep going; the row records the error
            row["error"] = repr(exc)[:300]
            continue
        trade = trades.get(condition) if role == "candidate" else None
        if price:
            row.update(price_utc=utc_text(price[0]), price=price[1], price_minutes=price[2],
                       price_age_minutes=round((snapshot - price[0]).total_seconds() / 60, 1))
        if trade:
            row.update(last_trade_utc=utc_text(trade[0], seconds=True), last_trade_price=trade[1],
                       minutes_since_trade=round((snapshot - trade[0]).total_seconds() / 60, 1))
    return rows


def main():
    global raw_log
    with CALENDAR.open(newline="") as handle:
        snapshots = {r["contest_id"]: stamp(r["snapshot_utc"]) for r in csv.DictReader(handle)
                     if r["platform"] == "Polymarket"}
    events = [e for e in json.loads(EVENTS.read_text()) if str(e["id"]) in snapshots]
    missing = set(snapshots) - {str(e["id"]) for e in events}
    if missing:
        raise SystemExit(f"{len(missing)} calendar contests are not in {EVENTS.name}, e.g. {sorted(missing)[:3]}")
    jobs = [(str(e["id"]), snapshots[str(e["id"])], e) for e in events]
    print(f"{sum(len(options(e)) for e in events)} options in {len(events)} contests")

    RAW.parent.mkdir(exist_ok=True)
    raw_log = gzip.open(RAW, "wt")
    rows = []
    try:
        with ThreadPoolExecutor(4) as pool:
            for i, contest in enumerate(pool.map(contest_rows, jobs), 1):
                rows += contest
                if i % 25 == 0 or i == len(jobs):
                    print(f"  {i}/{len(jobs)} contests", flush=True)
    finally:
        raw_log.close()

    order = {"candidate": 0, "basket": 1}
    rows.sort(key=lambda r: (int(r["contest_id"]) if r["contest_id"].isdigit() else 0, r["contest_id"],
                             order[r["role"]], r["market_id"], r["candidate"]))
    with OUT.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    errors = sum(bool(r["error"]) for r in rows)
    no_price = sum(not r["error"] and r.get("price") is None for r in rows)
    no_trade = sum(not r["error"] and r["role"] == "candidate" and not r.get("last_trade_utc") for r in rows)
    print(f"{len(rows)} options -> {OUT.name}: {no_price} without a price, {no_trade} candidates without a trade "
          f"in the day before, {errors} errors" + (" (run again)" if errors else ""))


if __name__ == "__main__":
    main()
