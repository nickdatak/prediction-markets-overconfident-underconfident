"""Drop the contests that break the selection rules after the first cleaning pass.

    python3 apply_fixes.py

Edits kalshi_binary_elections.json and kalshi_binary_decisions.csv in place and rewrites the
"Fixes applied" section at the end of kalshi_binary_report.txt. Safe to run more than once.
"""

import collections
import csv
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARKETS = HERE / "kalshi_binary_elections.json"
DECISIONS = HERE / "kalshi_binary_decisions.csv"
REPORT = HERE / "kalshi_binary_report.txt"

LOCKOUT = "drop: decided by the top-two primary leaving one party out, not by a vote between the two options"
CHAMBER = "drop: control of a whole chamber or parliament, not one race"
INTERNAL = "drop: internal party vote, not a public election"
NO_VOTE = "drop: nominee settled before any primary vote (market resolved before the state's primary date)"

FIXES = {
    **{f"KXHOUSERACE-CA{d}-26": LOCKOUT for d in ("04", "07", "11", "12", "14", "29", "34", "37")},
    "HOUSECA40-26": LOCKOUT,
    **{t: NO_VOTE for t in ("KXCTPRIMARY-02D26", "KXCTPRIMARY-02R26", "KXCTPRIMARY-03D26", "KXCTPRIMARY-03R26",
                            "KXCTPRIMARY-04D26", "KXVAPRIMARY-03D26", "KXVAPRIMARY-09R26")},
    "KXNJGA-25": CHAMBER,
    "KXMALAWIASSEMBLY-25": CHAMBER,
    "KXSERBIAPARLI-26JUN30": CHAMBER,
    "KXMALTAPARLI-26MAY30": CHAMBER,
    "KXCAPEVERDEPARLI-26MAY17": CHAMBER,
    "KXARGENTINASENATE-25OCT26": CHAMBER,
    "KXUKLABOURDEPUTY-25OCT25": INTERNAL,
    "KXGOPPOLICYCHAIR-26APR01": INTERNAL,
}
PRIMARY = re.compile(r"nominee|nomination|primary", re.I)
MARKER = "\n\nFixes applied after the first pass"

markets = json.loads(MARKETS.read_text())
kept = [m for m in markets if m["event_ticker"] not in FIXES]
MARKETS.write_text(json.dumps(kept))

with DECISIONS.open(newline="") as handle:
    reader = csv.DictReader(handle)
    fields, rows = reader.fieldnames, list(reader)
for row in rows:
    if row["event_ticker"] in FIXES:
        row["kept"], row["reason"] = "false", FIXES[row["event_ticker"]]
with DECISIONS.open("w", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)

events = collections.defaultdict(list)
for m in kept:
    events[m["event_ticker"]].append(m)
assert all(len(ms) == 2 and sorted(x["result"] for x in ms) == ["no", "yes"] for ms in events.values())
by_year = collections.Counter(min(x["open_time"] for x in ms)[:4] for ms in events.values())
primaries = sum(bool(PRIMARY.search(ms[0]["title"])) for ms in events.values())
by_reason = collections.Counter(FIXES.values())

lines = [MARKER.strip(), f"Events dropped by these fixes: {len(FIXES)}"]
lines += [f"  {count}\t{reason}" for reason, count in by_reason.most_common()]
lines += [f"Events kept now: {len(events)} ({len(kept)} markets)",
          f"  primaries {primaries}, other contests {len(events) - primaries}",
          "Kept events by year opened:"]
lines += [f"  {year}\t{count}" for year, count in sorted(by_year.items())]
report = REPORT.read_text().split(MARKER)[0]
REPORT.write_text(report.rstrip() + "\n\n" + "\n".join(lines) + "\n")
print("\n".join(lines))
