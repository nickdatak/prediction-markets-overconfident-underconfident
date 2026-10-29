"""Turn the pulled prices into one row per contest, saved to contest_prices.csv.

    python3 apply_rules.py

Run after kalshi_prices.py and polymarket_prices.py. None of the rules looks at who won.
  Both        the contest is dropped if any of its markets opened after the snapshot (a market made
              after the vote prices the count, not the election).
  Kalshi      a candidate's price is the midpoint of the best YES bid and ask when they are at most
              10 cents apart; otherwise the last trade, if it was in the 60 minutes before the
              snapshot; otherwise the contest is dropped.
  Polymarket  a candidate's price is the recorded price at the snapshot (no more than 60 minutes
              old). The contest is dropped if neither candidate traded in the 24 hours before the
              snapshot, or if "another candidate" is priced above 5%. When that option has no
              recorded price, 1 minus the two candidate prices stands in for it.
  Both        the prices of all options (the two candidates, plus "another candidate" when it has a
              recorded price) must add up to between 0.90 and 1.10; further off, at least one price
              is stale. Then the two candidate prices are scaled to sum to 1, and the contest is kept
              only if both scaled prices are between 5% and 95%.
Each dropped contest gets the first rule it fails. Also writes prices_report.txt.
"""

import collections
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CALENDAR = HERE / "contest_calendar.csv"
KALSHI = HERE / "kalshi_prices.csv"
POLYMARKET = HERE / "polymarket_prices.csv"
KALSHI_MARKETS = HERE.parent / "cleaning" / "kalshi_binary_elections.json"
POLYMARKET_EVENTS = HERE.parent / "cleaning" / "polymarket_binary_elections.json"
OUT = HERE / "contest_prices.csv"
REPORT = HERE / "prices_report.txt"

MAX_SPREAD = 0.10
MAX_TRADE_MINUTES = 60      # Kalshi: last trade standing in for a wide quote
MAX_PRICE_AGE_MINUTES = 60  # Polymarket: age of the recorded price point
MAX_BASKET = 0.05
MAX_TOTAL_GAP = 0.10        # all options must add up to within this of 1
BAND = (0.05, 0.95)
EPS = 1e-9

FIELDS = ["platform", "contest_id", "title", "kind", "country", "state", "election_key", "snapshot_utc",
          "candidate_a", "candidate_b", "price_a", "price_b", "priced_by_a", "priced_by_b", "spread_a", "spread_b",
          "minutes_since_trade_a", "minutes_since_trade_b", "basket_price", "basket_priced_by", "total",
          "p_a", "p_b", "winner", "a_won", "included", "reason"]


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def stamp(text):
    """Any of the time formats in the data to an aware datetime (Python 3.10 compatible)."""
    text = text.strip().replace("Z", "+00:00").replace(" ", "T", 1)
    if "T" in text and re.search(r"[+-]\d\d$", text):
        text += ":00"
    text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text)
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def read(path):
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def opened():
    """When the last market of each contest opened, by platform and contest id."""
    times = {"Kalshi": {}, "Polymarket": {}}
    for market in json.loads(KALSHI_MARKETS.read_text()):
        moment = stamp(market["open_time"])
        times["Kalshi"][market["event_ticker"]] = max(moment, times["Kalshi"].get(market["event_ticker"], moment))
    for event in json.loads(POLYMARKET_EVENTS.read_text()):
        starts = [stamp(m.get("startDate") or m["createdAt"]) for m in event["markets"]]
        times["Polymarket"][str(event["id"])] = max(starts)
    return times


def kalshi_price(market):
    """(price, how, spread) for one Kalshi market; price is None if no rule gives one."""
    bid, ask = number(market["yes_bid"]), number(market["yes_ask"])
    spread = ask - bid if bid is not None and ask is not None else None
    if spread is not None and 0 <= spread <= MAX_SPREAD + EPS:
        return (bid + ask) / 2, "midpoint", spread
    minutes, trade = number(market["minutes_since_trade"]), number(market["last_trade_price"])
    if minutes is not None and trade is not None and minutes <= MAX_TRADE_MINUTES:
        return trade, "last trade", spread
    return None, None, spread


def kalshi_contest(markets):
    row = {}
    if len(markets) != 2:
        return row, f"error: {len(markets)} markets pulled, expected 2 (run kalshi_prices.py again)"
    a, b = markets
    row.update(candidate_a=a["candidate"], candidate_b=b["candidate"],
               minutes_since_trade_a=a["minutes_since_trade"], minutes_since_trade_b=b["minutes_since_trade"],
               winner=a["candidate"] if a["result"] == "yes" else b["candidate"], a_won=int(a["result"] == "yes"))
    if a["error"] or b["error"]:
        return row, "error: API request failed (run kalshi_prices.py again)"
    (pa, how_a, spread_a), (pb, how_b, spread_b) = kalshi_price(a), kalshi_price(b)
    row.update(price_a=pa, price_b=pb, priced_by_a=how_a, priced_by_b=how_b, spread_a=spread_a, spread_b=spread_b)
    if pa is None or pb is None:
        return row, "drop: no quote within 10 cents and no trade in the hour before the snapshot"
    row["total"] = pa + pb
    return row, None


def polymarket_contest(options):
    row = {}
    candidates = [o for o in options if o["role"] == "candidate"]
    baskets = [o for o in options if o["role"] == "basket"]
    if len(candidates) != 2:
        return row, f"error: {len(candidates)} candidates pulled, expected 2 (run polymarket_prices.py again)"
    a, b = candidates
    row.update(candidate_a=a["candidate"], candidate_b=b["candidate"],
               minutes_since_trade_a=a["minutes_since_trade"], minutes_since_trade_b=b["minutes_since_trade"],
               winner=a["candidate"] if a["won"] == "true" else b["candidate"], a_won=int(a["won"] == "true"))
    if any(o["error"] for o in options):
        return row, "error: API request failed (run polymarket_prices.py again)"
    pa, pb = number(a["price"]), number(b["price"])
    row.update(price_a=pa, price_b=pb, priced_by_a="recorded price" if pa is not None else None,
               priced_by_b="recorded price" if pb is not None else None)
    if pa is None or pb is None:
        return row, "drop: no recorded price before the snapshot"
    if max(number(a["price_age_minutes"]), number(b["price_age_minutes"])) > MAX_PRICE_AGE_MINUTES:
        return row, "drop: recorded price more than an hour old at the snapshot"
    if not a["last_trade_utc"] and not b["last_trade_utc"]:
        return row, "drop: neither candidate traded in the 24 hours before the snapshot"
    row["total"] = pa + pb
    if baskets:
        basket = number(baskets[0]["price"])
        if basket is None:
            row.update(basket_price=max(0.0, 1 - pa - pb), basket_priced_by="1 minus the candidates")
        else:
            row.update(basket_price=basket, basket_priced_by="recorded price", total=pa + pb + basket)
        if row["basket_price"] > MAX_BASKET + EPS:
            return row, "drop: another candidate priced above 5%"
    return row, None


def main():
    calendar = read(CALENDAR)
    opened_at = opened()
    pulled = {"Kalshi": collections.defaultdict(list), "Polymarket": collections.defaultdict(list)}
    for platform, path in (("Kalshi", KALSHI), ("Polymarket", POLYMARKET)):
        for row in read(path):
            pulled[platform][row["contest_id"]].append(row)

    rows = []
    for contest in calendar:
        platform = contest["platform"]
        row = {key: contest[key] for key in ("platform", "contest_id", "title", "kind", "country", "state",
                                             "election_key", "snapshot_utc")}
        found = pulled[platform].get(contest["contest_id"], [])
        if not found:
            reason = "error: no prices pulled for this contest"
        else:
            extra, reason = (kalshi_contest if platform == "Kalshi" else polymarket_contest)(found)
            row.update(extra)
        if opened_at[platform][contest["contest_id"]] > stamp(contest["snapshot_utc"]):
            reason = "drop: market opened after the snapshot"
        if reason is None and abs(row["total"] - 1) > MAX_TOTAL_GAP + EPS:
            reason = "drop: prices of all options add up to more than 10 cents away from 1"
        if reason is None:
            pa, pb = row["price_a"], row["price_b"]
            if pa + pb <= 0:
                reason = "drop: both candidate prices are zero"
            else:
                row["p_a"], row["p_b"] = pa / (pa + pb), pb / (pa + pb)
                if not BAND[0] - EPS <= row["p_a"] <= BAND[1] + EPS:
                    reason = "drop: favourite above 95% (scaled)"
        row["included"] = "true" if reason is None else "false"
        row["reason"] = reason or "kept"
        for key in ("price_a", "price_b", "spread_a", "spread_b", "basket_price", "total", "p_a", "p_b"):
            if isinstance(row.get(key), float):
                row[key] = round(row[key], 6)
        rows.append(row)

    with OUT.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    lines = []
    for platform in ("Kalshi", "Polymarket"):
        mine = [r for r in rows if r["platform"] == platform]
        kept = [r for r in mine if r["included"] == "true"]
        kinds = collections.Counter(r["kind"] for r in kept)
        lines += [f"{platform}: {len(kept)} of {len(mine)} contests kept "
                  f"({kinds['primary']} primaries, {kinds['general']} other)"]
        lines += [f"  {count}\t{reason}" for reason, count in
                  collections.Counter(r["reason"] for r in mine if r["included"] != "true").most_common()]
        if platform == "Kalshi":
            how = collections.Counter(r[k] for r in kept for k in ("priced_by_a", "priced_by_b"))
            lines.append(f"  kept candidates priced by midpoint {how['midpoint']}, by last trade {how['last trade']}")
    lines.append(f"Both: {sum(r['included'] == 'true' for r in rows)} of {len(rows)} contests kept")
    errors = sum(r["reason"].startswith("error") for r in rows)
    if errors:
        lines.append(f"WARNING: {errors} contests have errors; run the pull again before using this table")
    REPORT.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"-> {OUT.name}, {REPORT.name}")


if __name__ == "__main__":
    main()
