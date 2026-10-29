"""Every closed Polymarket event carrying an election tag, with its markets, saved to
data/polymarket_elections.json.

    python3 polymarket_elections.py
    python3 polymarket_elections.py --open    # events still open instead, to data/polymarket_elections_open.json

An election tag is any tag whose name or slug contains "election". Polymarket spreads
elections over many such tags (the 2024 US Senate races, for example, carry "US Election" and
"Senate Elections" but not the main "Elections" tag), so the script starts from the main tag
and a few known ones, then keeps following every election tag it finds on the events it has
pulled until no new ones turn up.
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = "https://gamma-api.polymarket.com"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
OPEN = "--open" in sys.argv[1:]
OUT = Path(__file__).resolve().parent / "data" / ("polymarket_elections_open.json" if OPEN else "polymarket_elections.json")

# Election tags known to hold US races that the main "Elections" tag misses.
SEED_TAGS = {
    "1101": "US Election", "100199": "Senate Elections", "103899": "House Elections",
    "104094": "Governor Elections", "100164": "State Presidential Election", "377": "elections 2024",
    "185": "2024 election", "641": "2024 general election", "102788": "Mayoral Elections",
    "102006": "Special Elections", "902": "primary elections",
    # Not named "election", but it holds the 2024 House races ("NY-17 election: Jones vs Lawler").
    "100344": "House Races",
}


def get(path, params=None):
    url = f"{BASE}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    for attempt in range(7):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=60) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code not in (408, 425, 429, 500, 502, 503, 504) or attempt == 6:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 6:
                raise
        time.sleep(2 ** attempt)


def closed_events(tag_id):
    events, cursor, seen = [], None, set()
    while True:
        params = {"tag_id": tag_id, "closed": "false" if OPEN else "true", "include_markets": "true", "limit": 100}
        if cursor:
            params["after_cursor"] = cursor
        try:
            body = get("/events/keyset", params)
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404):  # tag no longer exists
                return events
            raise
        events += body.get("events") or []
        cursor = body.get("next_cursor")
        if not cursor or not body.get("events") or cursor in seen:
            return events
        seen.add(cursor)


def election_tags(event):
    for tag in event.get("tags") or []:
        name = f"{tag.get('label') or ''} {tag.get('slug') or ''}"
        if "election" in name.lower() and tag.get("id") is not None:
            yield str(tag["id"]), (tag.get("label") or tag.get("slug") or "").strip()


if __name__ == "__main__":
    main = get("/tags/slug/elections")
    names = {str(main["id"]): main.get("label") or "Elections", **SEED_TAGS}
    to_pull, pulled, events = list(names), set(), {}

    round_ = 0
    while to_pull:
        round_ += 1
        with ThreadPoolExecutor(6) as pool:
            batches = list(pool.map(closed_events, to_pull))
        pulled.update(to_pull)
        for batch in batches:
            for event in batch:
                events.setdefault(str(event["id"]), event)
        found = {}
        for event in events.values():
            found.update(election_tags(event))
        names.update({tag_id: name for tag_id, name in found.items() if tag_id not in names})
        to_pull = sorted(set(found) - pulled)
        print(f"round {round_}: {len(pulled)} tags pulled, {len(events)} events, {len(to_pull)} new tags")

    out = sorted(events.values(), key=lambda e: (e.get("endDate") or "", str(e["id"])))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out))
    markets = sum(len(e.get("markets") or []) for e in out)
    years = {}
    for event in out:
        year = (event.get("endDate") or "")[:4] or "none"
        years[year] = years.get(year, 0) + 1
    print(f"{len(out)} {'open' if OPEN else 'closed'} events, {markets} markets -> {OUT}")
    print("events by end year:", dict(sorted(years.items())))
    print(f"{len(pulled)} election tags used:", "; ".join(sorted(set(names[t] for t in pulled))))
