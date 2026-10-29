"""List the contests of the 2026 US midterms (3 November) before the vote, saved to midterm_contests.csv.

    python3 ../pull/kalshi_elections.py --open && python3 ../pull/polymarket_elections.py --open
    python3 list_midterm_contests.py

Reads  ../pull/data/kalshi_elections_open.json, ../pull/data/polymarket_elections_open.json
       midterm_decisions.csv   one row per candidate contest: is it one US race decided by the 3 November vote,
                               with a candidate or party as the winner? Judged on titles and expiry dates only.
                               Also names the race (office, state, district) and its state.
Writes midterm_candidates.csv  every open US election event that is not a primary, with its structure check
       midterm_contests.csv    the list: candidates with exactly two named options traded and a "keep" decision

These are the selection rules used for the training contests, except "one clear winner", which needs the result
and is checked after the vote. A race listed more than once on a platform is kept once: the listing whose options
are the candidates rather than the parties, then the one with more volume. Volume is used only for that and to tell
whether an option has traded; prices are not used.
The list is the universe of the midterm test: a market created after it is made is not part of the test.
"""

import collections
import csv
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "cleaning"))
from build_calendar import PRIMARY_WORDS, state_of  # noqa: E402
from clean_polymarket import BASKET, PLACEHOLDER, structure  # noqa: E402

KALSHI = HERE.parent / "pull" / "data" / "kalshi_elections_open.json"
POLYMARKET = HERE.parent / "pull" / "data" / "polymarket_elections_open.json"
DECISIONS = HERE / "midterm_decisions.csv"
PARTY = re.compile(r"^(the )?(republican|democratic|democrat|republicans|democrats)( party)?$", re.I)
CANDIDATES = HERE / "midterm_candidates.csv"
OUT = HERE / "midterm_contests.csv"


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def kalshi_events():
    events = collections.defaultdict(list)
    for market in json.loads(KALSHI.read_text()):
        events[market["event_ticker"]].append(market)
    for ticker, markets in events.items():
        names = [(m.get("yes_sub_title") or "").strip() for m in markets]
        traded = [n for m, n in zip(markets, names) if number(m.get("volume_fp") or m.get("volume")) > 0
                  and not BASKET.search(n) and not PLACEHOLDER.search(n)]
        volume = sum(number(m.get("volume_fp") or m.get("volume")) for m, n in zip(markets, names) if n in traded)
        yield {"platform": "Kalshi", "contest_id": ticker, "title": markets[0]["title"].strip(), "volume": volume,
               "markets": len(markets), "two_traded": len(traded) == 2, "options": " | ".join(traded),
               "structure": "two candidate markets" if len(traded) == 2 else f"{len(traded)} named options traded"}


def polymarket_events():
    for event in json.loads(POLYMARKET.read_text()):
        ok, reason, named, _ = structure(event)
        if ok and len(named) == 1:  # one market whose two outcomes are the candidates
            options = json.loads(named[0]["outcomes"]) if isinstance(named[0]["outcomes"], str) else named[0]["outcomes"]
        else:
            options = [(m.get("groupItemTitle") or m.get("question") or "").strip() for m in named]
        volume = sum(number(m.get("volumeNum") or m.get("volume")) for m in named)
        yield {"platform": "Polymarket", "contest_id": str(event["id"]), "title": (event.get("title") or "").strip(),
               "volume": volume, "markets": len(event.get("markets") or []), "two_traded": ok,
               "options": " | ".join(options),
               "structure": reason}


def main():
    candidates = []
    for event in [*kalshi_events(), *polymarket_events()]:
        state = state_of(event["title"], event["contest_id"])
        if state and not PRIMARY_WORDS.search(event["title"]):
            candidates.append(dict(event, state=state))
    fields = ["platform", "contest_id", "state", "title", "markets", "two_traded", "structure", "options", "volume"]
    with CANDIDATES.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(candidates, key=lambda c: (c["platform"], c["state"], c["contest_id"])))

    decisions = {}
    if DECISIONS.exists():
        with DECISIONS.open(newline="") as handle:
            decisions = {(r["platform"], r["contest_id"]): r for r in csv.DictReader(handle)}
    undecided = [c for c in candidates if c["two_traded"] and (c["platform"], c["contest_id"]) not in decisions]
    chosen = collections.defaultdict(list)
    for c in candidates:
        decision = decisions.get((c["platform"], c["contest_id"]), {})
        if c["two_traded"] and decision.get("keep") == "true":
            chosen[(c["platform"], decision["race"])].append(dict(c, race=decision["race"], state=decision["state"]))
    kept, duplicates = [], 0
    for listings in chosen.values():
        listings.sort(key=lambda c: (not all(PARTY.match(o) for o in c["options"].split(" | ")), c["volume"]),
                      reverse=True)
        kept.append(listings[0])
        duplicates += len(listings) - 1
    with OUT.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["platform", "contest_id", "race", "state", "title", "options"],
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(kept, key=lambda c: (c["platform"], c["state"], c["contest_id"])))

    by_platform = collections.Counter(c["platform"] for c in kept)
    print(f"{len(candidates)} open US non-primary events -> {CANDIDATES.name}; "
          f"{sum(c['two_traded'] for c in candidates)} with exactly two named options traded")
    print(f"{len(kept)} contests on the list -> {OUT.name} (Kalshi {by_platform['Kalshi']}, "
          f"Polymarket {by_platform['Polymarket']})")
    print(f"{duplicates} listings left out because the same race is listed more than once on that platform")
    if undecided:
        print(f"{len(undecided)} candidates have no decision in {DECISIONS.name} yet")


if __name__ == "__main__":
    main()
