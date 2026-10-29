"""Keep Polymarket election contests with exactly two named candidates and one clear winner.

    python3 clean_polymarket.py

Reads  ../pull/data/polymarket_elections.json
       polymarket_rule1_decisions.csv   which contests are a race winner (judged on titles)
Writes polymarket_binary_elections.json  kept events, with only their two candidate markets and
                                         any "Other" basket market, plus "basket_market_ids"
       polymarket_binary_decisions.csv   one row per event: kept or not, and why
       polymarket_binary_report.txt

Rules, applied in this order (an event's reason is the first rule it fails):
  1. Two candidates. Exactly two named options traded. A basket ("Other", "another party") is not
     a candidate, and placeholders that never traded ("Candidate A") are ignored. One market
     whose two outcomes are the two candidates also counts.
  2. A race winner. The title says the market pays on who wins one named race. Judged by reading
     the titles of every event that passes rule 1; the judgements are in the decisions CSV and
     never use prices, volume or results. Events missing from it are listed for review.
  3. One clear winner. Every market is closed and settled at exactly 1 or 0, and exactly one
     option won. A race won by a placeholder nobody traded is dropped and listed in the report,
     because that exclusion depends on the result. A basket win is kept (both candidates lose).
  4. One listing per race. Same race listed twice: keep the listing with more volume.

Volume is used only to tell whether an option ever traded and to break duplicate ties. Whether a
basket was small enough is checked later, from prices at the snapshot.
"""

import collections
import csv
import json
import random
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
INPUT = HERE.parent / "pull" / "data" / "polymarket_elections.json"
RULE1 = HERE / "polymarket_rule1_decisions.csv"
OUT_JSON = HERE / "polymarket_binary_elections.json"
OUT_CSV = HERE / "polymarket_binary_decisions.csv"
OUT_REPORT = HERE / "polymarket_binary_report.txt"

BASKET = re.compile(r"^\s*(other|others|another|any other|field|someone else|none of|a candidate from another|"
                    r"other candidate|other party)\b", re.I)
PLACEHOLDER = re.compile(r"^\s*(person|candidate|player|party|option)\s+[a-z0-9]{1,2}\s*$", re.I)
STOPWORDS = {"the", "a", "an", "of", "in", "for", "election", "winner", "who", "will", "win", "2024", "2025", "2026"}


def volume(market):
    try:
        return float(market.get("volumeNum") or market.get("volume") or 0)
    except (TypeError, ValueError):
        return 0.0


def as_list(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return []
    return value or []


def settled(market):
    """The market's payout vector as floats if it is exactly 1/0, else None."""
    try:
        prices = [float(p) for p in as_list(market.get("outcomePrices"))]
    except (TypeError, ValueError):
        return None
    return prices if sorted(prices) == [0.0, 1.0] else None


def yes_won(market):
    outcomes = [str(o).lower() for o in as_list(market.get("outcomes"))]
    return settled(market)[outcomes.index("yes")] == 1.0


def name_of(market):
    return (market.get("groupItemTitle") or "").strip() or re.sub(r"^Will (a |an )?", "", market.get("question") or "")


def is_basket(market):
    return bool(market.get("negRiskOther")) or bool(BASKET.search(name_of(market)))


def structure(event):
    """(ok, reason, candidate markets, basket markets)."""
    markets = event.get("markets") or []
    if not markets:
        return False, "no markets", [], []
    yes_no = [sorted(str(o).lower() for o in as_list(m.get("outcomes"))) == ["no", "yes"] for m in markets]
    if len(markets) == 1 and not yes_no[0] and len(as_list(markets[0].get("outcomes"))) == 2:
        if volume(markets[0]) <= 0:
            return False, "never traded", [], []
        return True, "one market, two named outcomes", markets, []
    if not all(yes_no):
        return False, "markets are not all yes/no", [], []
    baskets = [m for m in markets if is_basket(m)]
    named = [m for m in markets if m not in baskets and volume(m) > 0 and not PLACEHOLDER.search(name_of(m))]
    if len(named) != 2:
        return False, f"{min(len(named), 5)}{'+' if len(named) >= 5 else ''} named candidates traded (needs exactly 2)", [], []
    return True, "two candidate markets", named, baskets


def settlement(event, named, baskets):
    """(ok, reason, outcome_dependent)."""
    markets = event.get("markets") or []
    if not all(m.get("closed") for m in markets) or any(settled(m) is None for m in markets):
        return False, "not every market closed and settled at 1 or 0", False
    if len(markets) == 1:
        return True, "", False
    winners = [m for m in markets if yes_won(m)]
    if len(winners) != 1:
        return False, f"{len(winners)} winning options (needs exactly 1)", False
    winner = winners[0]
    if winner not in named and winner not in baskets:
        return False, "won by an option nobody traded", True
    return True, "", False


def race_key(event, named):
    words = sorted(w for w in re.findall(r"[a-z0-9]+", event.get("title", "").lower()) if w not in STOPWORDS)
    names = sorted(name_of(m).lower() for m in named) if len(named) == 2 else sorted(
        str(o).lower() for o in as_list(named[0].get("outcomes")))
    return (" ".join(words), tuple(names), (event.get("endDate") or "")[:10])


def main():
    events = json.loads(INPUT.read_text())
    with RULE1.open(newline="") as handle:
        rule1 = {row["event_id"]: row for row in csv.DictReader(handle)}

    decisions, kept, review, outcome_dependent = {}, {}, [], []
    for event in events:
        event_id = str(event["id"])
        ok, reason, named, baskets = structure(event)
        if ok:
            judged = rule1.get(event_id)
            if judged is None:
                ok, reason = False, "needs a title review (not in polymarket_rule1_decisions.csv)"
                review.append(event)
            elif judged["keep"].strip().lower() != "true":
                ok, reason = False, judged["reason"]
        if ok:
            ok, why, dependent = settlement(event, named, baskets)
            if not ok:
                reason = why
                if dependent:
                    outcome_dependent.append(event)
        if ok:
            kept[event_id] = (event, named, baskets)
            reason = "kept: two candidates, one winner, race winner"
        decisions[event_id] = (event, ok, reason)

    # Rule 4: one listing per race.
    by_race = collections.defaultdict(list)
    for event_id, (event, named, baskets) in kept.items():
        by_race[race_key(event, named)].append(event_id)
    duplicates = []
    for ids in by_race.values():
        if len(ids) > 1:
            total = {i: sum(volume(m) for m in kept[i][1]) for i in ids}
            keep = max(ids, key=lambda i: total[i])
            for i in ids:
                if i != keep:
                    duplicates.append((i, keep))
                    decisions[i] = (kept[i][0], False, f"duplicate listing of event {keep}")
                    del kept[i]

    out = []
    for event_id, (event, named, baskets) in kept.items():
        record = dict(event, markets=named + baskets, basket_market_ids=[str(m["id"]) for m in baskets])
        out.append(record)
    out.sort(key=lambda e: ((e.get("endDate") or ""), str(e["id"])))
    OUT_JSON.write_text(json.dumps(out))

    with OUT_CSV.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["event_id", "end_date", "title", "kept", "reason"])
        for event_id, (event, ok, reason) in sorted(decisions.items(), key=lambda item: int(item[0])):
            writer.writerow([event_id, (event.get("endDate") or "")[:10], event.get("title"), str(ok).lower(), reason])

    year = lambda e: (e.get("endDate") or "")[:4] or "none"
    by_reason = collections.Counter(reason for _, ok, reason in decisions.values() if not ok)
    by_year = collections.Counter(year(e) for e in out)
    with_basket = collections.Counter(year(e) for e in out if e["basket_market_ids"])
    rng = random.Random(20261029)
    dropped = [(e, r) for e, ok, r in decisions.values() if not ok]
    lines = [
        "Polymarket two-candidate election contests",
        f"Input: {INPUT.name} ({len(events)} events)",
        f"Kept: {len(out)}   Dropped: {len(events) - len(out)}",
        "",
        "Kept by end year (with an 'Other' basket market):",
        *[f"  {y}: {by_year[y]} ({with_basket.get(y, 0)})" for y in sorted(by_year)],
        "",
        "Dropped, by first rule failed:",
        *[f"  {n:5d}  {r}" for r, n in by_reason.most_common()],
        "",
        f"Dropped because an option nobody traded won (depends on the result, disclose): {len(outcome_dependent)}",
        *[f"  {e['id']} | {e.get('title')}" for e in outcome_dependent],
        "",
        f"Duplicate listings dropped: {len(duplicates)}",
        *[f"  {d} | {decisions[d][0].get('title')}  ->  kept {k}" for d, k in duplicates],
        "",
        f"Need a title review before they can be kept: {len(review)}",
        *[f"  {e['id']} | {(e.get('endDate') or '')[:10]} | {e.get('title')}" for e in review],
        "",
        "30 random kept:",
        *[f"  {e['id']} | {e.get('title')}" for e in rng.sample(out, min(30, len(out)))],
        "",
        "30 random dropped:",
        *[f"  {e['id']} | {e.get('title')} | {r}" for e, r in rng.sample(dropped, min(30, len(dropped)))],
    ]
    OUT_REPORT.write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:4 + len(by_year) + 2]))
    print(f"needs title review: {len(review)}, duplicates dropped: {len(duplicates)}, "
          f"won by an untraded option: {len(outcome_dependent)}")


if __name__ == "__main__":
    main()
