"""Election date and price-snapshot time for every kept contest on Kalshi and Polymarket.

    python3 build_calendar.py

Reads   ../cleaning/kalshi_binary_elections.json, ../cleaning/polymarket_binary_elections.json
        us_primary_dates_2026.csv        FEC table of 2026 state primary and runoff dates
        us_other_elections.csv           US specials, local races and 2023-24 contests, one row each
        international_election_days.csv  one row per election day outside the US
Writes  contest_calendar.csv   one row per contest: election date, snapshot time (UTC), how it was dated
        election_days.csv      one row per election day: time zone, snapshot rule, snapshot time

Snapshot rule (the moment each contest is priced):
  US       17:00 local time in the easternmost time zone among all states voting that day. No US
           state closes its polls before 18:00 local time, so this is always at least an hour
           before the first results anywhere in the country.
  Elsewhere  one hour before the first polls close in the country on the last voting day. Where
           the closing time could not be confirmed, 12:00 local time, which is before any closing.
"""

import collections
import csv
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
CLEANING = HERE.parent / "cleaning"

STATE_TZ = {  # easternmost time zone in each state, which closes first
    **dict.fromkeys("CT DE DC FL GA IN KY ME MD MA MI NH NJ NY NC OH PA RI SC TN VT VA WV".split(), "America/New_York"),
    **dict.fromkeys("AL AR IL IA KS LA MN MS MO NE ND OK SD TX WI".split(), "America/Chicago"),
    **dict.fromkeys("CO ID MT NM UT WY".split(), "America/Denver"),
    "AZ": "America/Phoenix", "OR": "America/Boise",
    **dict.fromkeys("CA NV WA".split(), "America/Los_Angeles"),
    "AK": "America/Anchorage", "HI": "Pacific/Honolulu",
}
STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
CITIES = {"Washington DC": "DC", "Charlotte": "NC", "Cincinnati": "OH", "Cleveland": "OH", "Miami": "FL",
          "Pittsburgh": "PA", "Sioux Falls": "SD", "Ann Arbor": "MI", "NYC": "NY", "Bronx": "NY",
          "King County": "WA", "Worcester": "MA", "Oakland": "CA", "Chandler": "AZ", "Providence": "RI"}
TICKER_STATE = [
    r"^(?:PRESPARTY|HOUSE|SENATE|GOVPARTY)([A-Z]{2})", r"^KX([A-Z]{2})PRIMARY", r"^KXSENATE([A-Z]{2})[RD]-",
    r"^KXGOV([A-Z]{2})NOM", r"^KX([A-Z]{2})\d+[RD]-", r"^KXAGNOM([A-Z]{2})[RD]-", r"^KXHOUSERACE-([A-Z]{2})\d",
    r"^KX([A-Z]{2})(?:SOS|TREAS)", r"^KX(?:SUPT|LABOR|INSCOM)([A-Z]{2})NOM",
]
NAMES = sorted(STATES.items(), key=lambda item: -len(item[1]))
PRIMARY_WORDS = re.compile(r"nominee|nomination|primary", re.I)
OLD_2024 = re.compile(r"^(PRESPARTY|HOUSE|SENATE|GOVPARTY)[A-Z].*-24$")
GENERAL_2025 = {
    "GOVPARTYNJ-25", "GOVPARTYVA-25", "KXATTYGENVA-25", "KXLTGOVVA-25", "KXMAYORCLEV-25", "KXMAYORCIN-25",
    "KXMAYORPITT-25", "KXMAYORCHAR-25", "KXKINGEXEC-25", "KXNYCCOUNCIL17-25", "KXNYCCOUNCIL38-25",
    "KXNYCCOUNCIL39-25", "KXNYCBRONXPRES-25", "KXGAPUBLICSERVICE2-25NOV04", "KXGAPUBLICSERVICE3-25NOV04",
    "KXPACOMMONWEALTHCOURT-25SEP24", "KXPASUPERIORCOURT-25NOV04", "KXMAYORWORCESTER-26",
    "28830", "54928", "33859",
}
INTERNATIONAL = {  # election-day key -> contests (Kalshi event tickers or Polymarket event ids)
    "CA-2025-04-28": "KXCLOVERDALE-25 KXCUMBERLAND-25 KXEDMONTONGATEWAY-25 KXKILDONAN-25 KXKITCHENER-25 "
                     "KXLANGLEY-25 KXLAPRAIRIE-25 KXNIAGARASOUTH-25 KXREGINAWASCANA-25 KXSOUTHSURREY-25 "
                     "KXWINNIPEGCENTRE-25",
    "CA-2026-08-31": "770149",
    "CA-BC-2026-10-17": "KXABBOTSFORDMAYOR-26OCT17 826324 826327 905554 905555 905556 905557 905558",
    "CA-ON-2026-10-26": "KXOSHAWAMAYOR-26OCT26 905572",
    "CA-MB-2026-10-28": "905679",
    "BR-2024-10-27": "13456 13458 13459",
    "BR-2026-10-04": "KXPARAIBAGOV-26OCT04",
    "BR-2026-10-25": "KXESGOV-26OCT25 KXTOCANTINSGOV-26OCT25",
    "KR-2026-06-03": "KXDAEGUMAYOR-26JUN03 410826 410827 410995 452754 479282",
    "IT-2026-06-08": "548887 548895 548896 548897 548913 548914 548917 548925 548926 548927 548928 548929 "
                     "548930 548938 548953 548954 548962",
    "FR-2026-03-22": "166322 248117",
    "TR-2024-03-31": "903344",
    "MK-2024-05-08": "10499",
    "BA-2025-11-23": "80661",
    "BJ-2026-04-12": "287579",
    "GH-2024-12-07": "KXPRESGHANA",
    "UY-2024-11-24": "KXPRESURUGUAY",
    "GY-2025-09-01": "KXGUYANAPRES-25",
    "MW-2025-09-16": "KXMALAWIPRES-25",
    "SC-2025-10-11": "KXSEYCHELLESPRES-25",
    "ZM-2026-08-13": "KXZAMBIAPRES-26",
    "SK-2026-10-24": "943055 943057",
    "AU-NT-2026-09-12": "943559",
    "AU-TAS-2026-10-27": "960893 960903",
}
CONTEST_TO_INTERNATIONAL = {cid: key for key, ids in INTERNATIONAL.items() for cid in ids.split()}
NATIONAL_2024 = {"4076", "14267"}
# Other states voting on pre-2026 days, so the snapshot respects the first closing in the country.
OTHER_STATES_VOTING = {
    "2023-11-07": "KY MS NJ VA",   # 2023 off-year general elections
    "2024-06-18": "OK UT VA",      # 2024 primaries
    "2024-06-25": "CO NY UT",
    "2024-08-13": "CT MN VT WI",
}  # national presidential winner by party; CO-8 (end date set to Dec 31)


def read_csv(name):
    with (HERE / name).open(newline="") as handle:
        return list(csv.DictReader(handle))


def day(text):
    return date.fromisoformat(text[:10]) if text else None


def stamp(text):
    """Timestamp from the API's formats ('...Z', '...+00', '... 10:04:30+00')."""
    text = text.replace(" ", "T").replace("Z", "+00:00")
    if re.search(r"[+-]\d\d$", text):
        text += ":00"
    text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text)  # Python 3.10 wants 6 digits
    return datetime.fromisoformat(text)


def state_of(title, contest_id):
    for city, code in CITIES.items():
        if city in title:
            return code
    found = re.search(r"\b([A-Z]{2})-(?:\d+|AL)\b|^([A-Z]{2}) House", title)
    if found and (found.group(1) or found.group(2)) in STATES:
        return found.group(1) or found.group(2)
    for pattern in TICKER_STATE:
        found = re.match(pattern, contest_id)
        if found and found.group(1) in STATES:
            return found.group(1)
    for code, name in NAMES:
        if re.search(rf"\b{name}\b", title):
            return code
    return None


def load_contests():
    contests = []
    events = collections.defaultdict(list)
    for market in json.loads((CLEANING / "kalshi_binary_elections.json").read_text()):
        events[market["event_ticker"]].append(market)
    for ticker, markets in events.items():
        contests.append({
            "platform": "Kalshi", "contest_id": ticker, "title": markets[0]["title"].strip(),
            "resolved": day(min(m["close_time"] for m in markets)), "market_end": None,
            "first_market_close": min(stamp(m["close_time"]) for m in markets),
        })
    for event in json.loads((CLEANING / "polymarket_binary_elections.json").read_text()):
        baskets = set(event.get("basket_market_ids") or [])
        closes = [stamp(m["closedTime"]) for m in event["markets"] if str(m["id"]) not in baskets and m.get("closedTime")]
        contests.append({
            "platform": "Polymarket", "contest_id": str(event["id"]), "title": event["title"].strip(),
            "resolved": day(event.get("closedTime")), "market_end": day(event.get("endDate")),
            "first_market_close": min(closes) if closes else None,
        })
    return contests


def snapshot_us(key_date, states):
    """17:00 in the easternmost zone among the states voting that day."""
    def offset(state):
        return datetime(key_date.year, key_date.month, key_date.day, 12, tzinfo=ZoneInfo(STATE_TZ[state])).utcoffset()
    eastern = max(states, key=offset)
    tz = STATE_TZ[eastern]
    local = datetime(key_date.year, key_date.month, key_date.day, 17, tzinfo=ZoneInfo(tz))
    return tz, local


def main():
    fec = read_csv("us_primary_dates_2026.csv")
    fec_by_state = collections.defaultdict(list)
    for row in fec:
        fec_by_state[row["state"]].append((day(row["date"]), row["type"], row["note"]))
    other = {row["contest_id"]: row for row in read_csv("us_other_elections.csv")}
    abroad = {row["key"]: row for row in read_csv("international_election_days.csv")}

    rows, problems = [], []
    for c in load_contests():
        cid, title = c["contest_id"], c["title"]
        kind = "primary" if PRIMARY_WORDS.search(title) else "general"
        row = dict(platform=c["platform"], contest_id=cid, title=title, kind=kind, country="United States",
                   state="", election_key="", election_date="", how_dated="", source="",
                   market_end_date=str(c["market_end"] or ""), market_resolved=str(c["resolved"] or ""), flag="")
        if cid in CONTEST_TO_INTERNATIONAL:
            info = abroad[CONTEST_TO_INTERNATIONAL[cid]]
            row.update(country=info["country"], election_key=info["key"], election_date=info["last_voting_day"],
                       how_dated="international election day table", source=info["source"])
        elif cid in other:
            info = other[cid]
            row.update(state=info["state"], election_date=info["date"], how_dated=f"listed: {info['note']}",
                       source=info["source"])
        elif OLD_2024.match(cid) or cid in NATIONAL_2024 or (
                c["platform"] == "Polymarket" and kind == "general" and str(c["market_end"]) in ("2024-11-04", "2024-11-05")):
            row.update(state=state_of(title, cid) or ("national" if cid == "4076" else ""),
                       election_date="2024-11-05", how_dated="2024 US general election")
        elif cid in GENERAL_2025:
            row.update(state=state_of(title, cid) or "", election_date="2025-11-04",
                       how_dated="2025 US general election (4 November)")
        elif kind == "primary":
            state = state_of(title, cid)
            dates = sorted(d for d, _, _ in fec_by_state.get(state, []) if c["resolved"] and d <= c["resolved"])
            if not state or not dates:
                problems.append(f"no FEC primary date for {c['platform']} {cid} ({state}) {title}")
                rows.append(row)
                continue
            chosen = dates[-1]
            note = next(f"{t} {n}".strip() for d, t, n in fec_by_state[state] if d == chosen)
            row.update(state=state, election_date=str(chosen), how_dated=f"FEC 2026 {state} {note}; latest vote before the market resolved",
                       source="https://www.fec.gov/help-candidates-and-committees/dates-and-deadlines/2026-reporting-dates/electioneering-communications-periods-congressional-primaries-2026/")
        else:
            problems.append(f"undated {c['platform']} {cid} {title}")
            rows.append(row)
            continue
        if row["country"] == "United States":
            row["election_key"] = f"US-{row['election_date']}"
        # Cross-checks against what the market itself says.
        election = day(row["election_date"])
        flags = []
        if c["resolved"] and c["resolved"] < election:
            flags.append("market resolved before the vote")
        if c["market_end"] and abs((c["market_end"] - election).days) > 1 and c["market_end"] < election + timedelta(days=60):
            flags.append(f"market end date {c['market_end']} differs from the election date")
        row["flag"] = "; ".join(flags)
        rows.append(row)

    # Election days and snapshots.
    states_by_day = collections.defaultdict(set)
    for r in rows:
        if r["election_key"].startswith("US-") and r["state"] in STATE_TZ:
            states_by_day[r["election_key"]].add(r["state"])
    for d, entries in ((day(r["date"]), r) for r in fec):
        states_by_day[f"US-{d}"].add(entries["state"])
    for d, states in OTHER_STATES_VOTING.items():
        states_by_day[f"US-{d}"].update(states.split())
    days = {}
    for key, states in states_by_day.items():
        if not any(r["election_key"] == key for r in rows):
            continue
        d = date.fromisoformat(key[3:])
        tz, local = snapshot_us(d, states)
        days[key] = dict(key=key, country="United States", place=" ".join(sorted(states)), last_voting_day=str(d),
                         timezone=tz, first_close_local="18:00 or later", rule="17:00 in the easternmost zone voting that day",
                         snapshot_local=local.strftime("%Y-%m-%d %H:%M %Z"),
                         snapshot_utc=local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%MZ"),
                         closing_verified="yes", source="https://ballotpedia.org/State_Poll_Opening_and_Closing_Times")
    for key, info in abroad.items():
        d = date.fromisoformat(info["last_voting_day"])
        tz = ZoneInfo(info["timezone"])
        if info["first_close_local"]:
            h, m = map(int, info["first_close_local"].split(":"))
            local = datetime(d.year, d.month, d.day, h, m, tzinfo=tz) - timedelta(hours=1)
            rule = "one hour before the first polls close"
        else:
            local = datetime(d.year, d.month, d.day, 12, tzinfo=tz)
            rule = "12:00 local (closing time not confirmed)"
        days[key] = dict(key=key, country=info["country"], place=info["place"], last_voting_day=info["last_voting_day"],
                         timezone=info["timezone"], first_close_local=info["first_close_local"], rule=rule,
                         snapshot_local=local.strftime("%Y-%m-%d %H:%M %Z"),
                         snapshot_utc=local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%MZ"),
                         closing_verified=info["closing_verified"], source=info["source"])
    counts = collections.Counter((r["election_key"], r["platform"]) for r in rows)
    for key, info in days.items():
        info["kalshi_contests"] = counts.get((key, "Kalshi"), 0)
        info["polymarket_contests"] = counts.get((key, "Polymarket"), 0)
    by_id = {(c["platform"], c["contest_id"]): c for c in load_contests()}
    for r in rows:
        r["snapshot_utc"] = days.get(r["election_key"], {}).get("snapshot_utc", "")
        first_close = by_id[(r["platform"], r["contest_id"])]["first_market_close"]
        if r["snapshot_utc"] and first_close and first_close <= datetime.fromisoformat(r["snapshot_utc"].replace("Z", "+00:00")):
            r["flag"] = "; ".join(filter(None, [r["flag"], f"a candidate's market closed before the snapshot ({first_close:%Y-%m-%d %H:%M} UTC)"]))

    fields = ["platform", "contest_id", "title", "kind", "country", "state", "election_key", "election_date",
              "snapshot_utc", "how_dated", "source", "market_end_date", "market_resolved", "flag"]
    with (HERE / "contest_calendar.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: (r["election_date"], r["platform"], r["contest_id"])))
    day_fields = ["key", "country", "place", "last_voting_day", "timezone", "first_close_local", "rule",
                  "snapshot_local", "snapshot_utc", "closing_verified", "kalshi_contests", "polymarket_contests", "source"]
    with (HERE / "election_days.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=day_fields)
        writer.writeheader()
        writer.writerows(sorted((v for v in days.values() if v["kalshi_contests"] or v["polymarket_contests"]),
                                key=lambda v: v["last_voting_day"]))

    dated = [r for r in rows if r["snapshot_utc"]]
    print(f"{len(rows)} contests, {len(dated)} dated, on {len({r['election_key'] for r in dated})} election days")
    print("unconfirmed closing times (noon snapshot):",
          sum(1 for r in dated if days[r["election_key"]]["closing_verified"] != "yes"), "contests")
    flagged = [r for r in rows if r["flag"]]
    print(f"{len(flagged)} contests flagged:")
    for r in flagged:
        print(f"  {r['platform']} {r['contest_id']} | {r['title'][:60]} | {r['election_date']} | {r['flag']}")
    if problems:
        print(f"{len(problems)} contests could not be dated:")
        for p in problems:
            print("  " + p)


if __name__ == "__main__":
    main()
