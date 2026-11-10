"""Polymarket prices and last trade at each contest's snapshot, saved to polymarket_prices.csv.

    python3 polymarket_prices.py
    python3 polymarket_prices.py --midterms   # the 2026 midterms, into midterm_* files; the frozen files are untouched

For each option of every Polymarket contest in contest_calendar.csv (the two candidates and, where
there is one, the "another candidate" basket):
  price   the last point of Polymarket's recorded price history at or before the snapshot, looked
          for at 1-minute spacing over the 6 hours before, then hourly over 2 days. The history has
          a point every minute even when nothing trades, so it says nothing about staleness.
  trade   (candidates only) the last trade in the candidate's market at or before the snapshot, if it
          was in the day before. Polymarket retired the version 1 trades endpoint on 24 October 2026 (it
          answers 410 Gone); version 2 ignores time limits for a market, so its trades are paged through
          until the walk passes the snapshot. Version 2 leaves out trades under 0.01 shares.
Every API response is kept in raw/polymarket_responses.jsonl.gz. Standard library only.
"""

import csv
import gzip
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
MIDTERMS = "--midterms" in sys.argv[1:]
EVENTS = HERE.parent / "cleaning" / ("midterm_polymarket_binary.json" if MIDTERMS else "polymarket_binary_elections.json")
CALENDAR = HERE / ("midterm_calendar.csv" if MIDTERMS else "contest_calendar.csv")
OUT = HERE / ("midterm_polymarket_prices.csv" if MIDTERMS else "polymarket_prices.csv")
RAW = HERE / "raw" / ("midterm_polymarket_responses.jsonl.gz" if MIDTERMS else "polymarket_responses.jsonl.gz")

CLOB = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
           "Accept": "application/json"}
PRICE_SEARCH = [(1, timedelta(hours=6)), (60, timedelta(days=2))]
TRADE_SEARCH = [(timedelta(0), timedelta(hours=1)), (timedelta(hours=1), timedelta(days=1))]
TRADE_PAGE = 1000
MAX_TRADE_PAGES = 500  # a safety stop: 500,000 trades in one market
FIELDS = ["contest_id", "market_id", "role", "candidate", "won", "snapshot_utc", "price_utc", "price_age_minutes",
          "price", "price_minutes", "last_trade_utc", "last_trade_price", "minutes_since_trade", "token_id",
          "condition_id", "error"]

RAW_LOCK = threading.Lock()
raw_log = None
GAP = 0.05  # seconds between requests, all threads together

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


class NotFound(Exception):
    pass


def get(url, params):
    url = f"{url}?{urllib.parse.urlencode(params)}"
    for attempt in range(10):
        wait_turn(GAP)
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
            if exc.code not in (403, 408, 425, 429, 500, 502, 503, 504) or attempt == 9:
                raise
            if exc.code == 429:
                pause_all(retry_wait(exc, attempt))  # the next wait_turn waits it out
            else:
                time.sleep(retry_wait(exc, attempt))
        except (OSError, ValueError):  # timeouts, dropped connections, cut-off responses
            if attempt == 9:
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


def last_trade(condition_id, snapshot):
    """The last trade at or before the snapshot, if it was in the day before (TRADE_SEARCH's widest window)."""
    cutoff, earliest = unix(snapshot), unix(snapshot - TRADE_SEARCH[-1][1])
    filters = {"condition": condition_id, "limit": TRADE_PAGE, "taker_only": "true"}  # sent again on every page
    best, cursor = None, None
    for _ in range(MAX_TRADE_PAGES):
        try:
            body = get(f"{DATA_API}/v2/trades", dict(filters, cursor=cursor) if cursor else filters) or {}
        except NotFound:
            return None
        trades = [(epoch(t["timestamp"]), number(t.get("price"))) for t in (body.get("data") or [])
                  if t.get("timestamp") is not None]
        at_or_before = [t for t in trades if t[0] <= cutoff]
        if at_or_before:
            best = max([t for t in (best, *at_or_before) if t], key=lambda t: t[0])
        if trades:  # stop once the walk has passed the snapshot, whichever way the feed runs
            newest_first = trades[0][0] >= trades[-1][0]
            if (at_or_before if newest_first else any(t[0] > cutoff for t in trades)):
                break
        cursor = (body.get("pagination") or {}).get("next_cursor")
        if not cursor:
            break
    else:
        raise RuntimeError(f"more than {MAX_TRADE_PAGES} pages of trades for {condition_id}")
    if best and best[0] >= earliest:
        return datetime.fromtimestamp(best[0], timezone.utc), best[1]
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
