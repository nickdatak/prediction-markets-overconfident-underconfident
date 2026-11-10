"""Apply the last selection rule to the midterm list after the vote, and date the kept contests.

    python3 cleaning/clean_midterms.py

Reads  ../calendar_and_prices/midterm_contests.csv   the list made before the vote (the test's universe)
       ../pull/data/midterms_kalshi.json, ../pull/data/midterms_polymarket.json   from pull/midterm_markets.py
Writes midterm_kalshi_binary.json        the two candidate markets of each kept Kalshi contest
       midterm_polymarket_binary.json    each kept Polymarket event: its two candidates and any "Other" option
       midterm_decisions_after_vote.csv  one row per listed contest: kept or not, and why
       ../calendar_and_prices/midterm_calendar.csv   kept contests, priced at the 3 November snapshot

The list already applied every selection rule except "one clear winner", which needs the result:
  Kalshi      every market of the event has a result, exactly one says yes, and it is one of the two listed
              candidates
  Polymarket  the same structure as when the list was made (the two listed candidates are still the only two
              traded), every market closed at exactly 1 or 0, and one winner (clean_polymarket.py's rules)
A contest not settled yet is left out of this run and counted. The frozen training files are not touched.
"""

import collections
import csv
import json
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "calendar_and_prices")]
from build_calendar import STATE_TZ, snapshot_us  # noqa: E402
from clean_polymarket import as_list, name_of, settlement, structure  # noqa: E402

LIST = HERE.parent / "calendar_and_prices" / "midterm_contests.csv"
KALSHI_IN = HERE.parent / "pull" / "data" / "midterms_kalshi.json"
POLYMARKET_IN = HERE.parent / "pull" / "data" / "midterms_polymarket.json"
KALSHI_OUT = HERE / "midterm_kalshi_binary.json"
POLYMARKET_OUT = HERE / "midterm_polymarket_binary.json"
DECISIONS = HERE / "midterm_decisions_after_vote.csv"
CALENDAR = HERE.parent / "calendar_and_prices" / "midterm_calendar.csv"

DAY, KEY = date(2026, 11, 3), "US-2026-11-03"
NOT_SETTLED = "not settled yet"
FIELDS = ["platform", "contest_id", "title", "kind", "country", "state", "election_key", "election_date",
          "snapshot_utc", "how_dated", "source", "market_end_date", "market_resolved", "flag"]


def names(text):
    return {n.strip().lower() for n in text.split(" | ")}


def kalshi(contest, markets):
    listed = names(contest["options"])
    candidates = [m for m in markets if (m.get("yes_sub_title") or "").strip().lower() in listed]
    if len(candidates) != 2:
        return f"drop: {len(candidates)} of the two listed candidates found", []
    if any(m.get("result") not in ("yes", "no") for m in markets):
        return NOT_SETTLED, []
    winners = [m for m in markets if m["result"] == "yes"]
    if len(winners) != 1:
        return f"drop: {len(winners)} winning options (needs exactly 1)", []
    if winners[0] not in candidates:
        return "drop: won by an option other than the two listed candidates", []
    return "kept", candidates


def polymarket(contest, event):
    ok, reason, named, baskets = structure(event)
    if not ok:
        return f"drop: structure changed since the list ({reason})", None
    found = (names(" | ".join(str(o) for o in as_list(named[0]["outcomes"]))) if len(named) == 1
             else {name_of(m).lower() for m in named})
    if found != names(contest["options"]):
        return "drop: the two traded candidates are not the two listed", None
    ok, reason, _ = settlement(event, named, baskets)
    if not ok:
        return (NOT_SETTLED if reason.startswith("not every market") else f"drop: {reason}"), None
    kept = dict(event, markets=named + baskets, basket_market_ids=[m["id"] for m in baskets])
    return "kept", kept


def main():
    with LIST.open(newline="") as handle:
        listed = list(csv.DictReader(handle))
    by_event = collections.defaultdict(list)
    for market in json.loads(KALSHI_IN.read_text()):
        by_event[market["event_ticker"]].append(market)
    events = {str(e["id"]): e for e in json.loads(POLYMARKET_IN.read_text())}

    tz, local = snapshot_us(DAY, list(STATE_TZ))
    snapshot = local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%MZ")
    assert snapshot == "2026-11-03T22:00Z", snapshot

    decisions, kalshi_kept, poly_kept, calendar = [], [], [], []
    for contest in listed:
        platform, cid = contest["platform"], contest["contest_id"]
        end = resolved = ""
        if platform == "Kalshi":
            markets = by_event.get(cid)
            reason, keep = kalshi(contest, markets) if markets else ("drop: not found after the vote", [])
            kalshi_kept += keep
            if keep:
                end = min(m.get("close_time") or "" for m in keep)[:10]
                resolved = max(m.get("settlement_ts") or m.get("close_time") or "" for m in keep)[:10]
        else:
            event = events.get(cid)
            reason, keep = polymarket(contest, event) if event else ("drop: not found after the vote", None)
            if keep:
                poly_kept.append(keep)
                end = (keep.get("endDate") or "")[:10]
                resolved = max((m.get("closedTime") or "") for m in keep["markets"])[:10]
        decisions.append({"platform": platform, "contest_id": cid, "state": contest["state"], "reason": reason,
                          "title": contest["title"]})
        if reason == "kept":
            calendar.append({"platform": platform, "contest_id": cid, "title": contest["title"], "kind": "general",
                             "country": "United States", "state": contest["state"], "election_key": KEY,
                             "election_date": DAY.isoformat(), "snapshot_utc": snapshot,
                             "how_dated": "2026 US midterms (3 November); on the list made before the vote",
                             "source": "midterm_contests.csv", "market_end_date": end, "market_resolved": resolved,
                             "flag": ""})

    KALSHI_OUT.write_text(json.dumps(kalshi_kept))
    POLYMARKET_OUT.write_text(json.dumps(poly_kept))
    for path, rows, fields in ((DECISIONS, decisions, ["platform", "contest_id", "state", "reason", "title"]),
                               (CALENDAR, calendar, FIELDS)):
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    for platform in ("Kalshi", "Polymarket"):
        mine = [d for d in decisions if d["platform"] == platform]
        counts = collections.Counter(d["reason"] for d in mine)
        print(f"{platform}: {counts['kept']} of {len(mine)} listed contests kept")
        for reason, count in counts.most_common():
            if reason != "kept":
                print(f"    {count:4d}  {reason}")
    print(f"-> {KALSHI_OUT.name}, {POLYMARKET_OUT.name}, {DECISIONS.name}, {CALENDAR.name} (snapshot {snapshot})")


if __name__ == "__main__":
    main()
