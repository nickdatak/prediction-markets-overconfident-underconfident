"""The pre-registered test on the US midterms of 3 November 2026. Written and committed before the vote.

    python3 midterm_test.py --stage preliminary   # on or after 10 November 2026
    python3 midterm_test.py --stage final         # every eligible contest settled, or on/after 31 January 2027
    python3 midterm_test.py --self-test           # runs the test on 5 November 2024; must reproduce the baseline

Run from the fit/ folder, after rebuilding ../calendar_and_prices/contest_prices.csv with the same pipeline.

Contests
    US general-election contests voted on 3 November 2026, on each platform separately, that pass every selection and
    price rule (included in contest_prices.csv), priced at 22:00 UTC on 3 November. Every office counts. Contests in
    Georgia, Louisiana and Mississippi are excluded whatever happens, because a race there can be decided by a later
    runoff instead of the 3 November vote.

Two questions per platform (stretch b, one row per contest, from the favourite's side; see stretch.py)
    Level   the midterm b, with a 95% interval from resampling states (2,000 resamples, seed 20261101).
            Interval entirely above 1: underconfident. Entirely below 1: overconfident. Otherwise: no detectable
            miscalibration.
    Change  the same interval against the frozen b of the general elections of 5 November 2024 (Kalshi 1.972,
            Polymarket 1.828), treated as a fixed value. Interval entirely below it: lower than 2024. Entirely above
            it: higher than 2024. Otherwise: consistent with 2024.
    An unlimited b (every favourite won) is kept as it is; verdicts read only the ends of the interval.

Reading, per platform
    underconfident, and consistent with or higher than 2024 -> underconfidence persists in general elections:
                                                               supports "primaries are traded differently"
    no detectable miscalibration or overconfident, and lower than 2024 -> underconfidence has not persisted:
                                                               supports "it switched off over time"
    underconfident, and lower than 2024                     -> still underconfident but less than in 2024: partial
                                                               support for "it switched off over time"
    anything else                                         -> inconclusive

Stages
    preliminary  run on or after 10 November 2026 on the contests settled by then; reported as preliminary, with the
                 number of contests still unsettled. Races still uncalled are more often ones where the favourite
                 underperformed, so this run leans towards "underconfident".
    final        run once every eligible contest has settled, or on or after 31 January 2027, whichever comes first;
                 contests unsettled then are left out. The reading above is judged on this run only.
Eligible contests before the vote are listed in ../calendar_and_prices/midterm_contests.csv when that file exists.
"""

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import stretch

TEST_DAY, TEST_SNAPSHOT = "US-2026-11-03", "2026-11-03T22:00Z"
BASELINE_DAY, BASELINE_SNAPSHOT = "US-2024-11-05", "2024-11-05T22:00Z"
BASELINE_B = {"Kalshi": 1.9716200931017527, "Polymarket": 1.8277906803393624}  # fit/results/*_baseline_2024.csv
RUNOFF_STATES = {"GA", "LA", "MS"}
PRELIMINARY_FROM, FINAL_FROM = date(2026, 11, 10), date(2027, 1, 31)
MIN_CONTESTS = 5
ELIGIBLE = Path("../calendar_and_prices/midterm_contests.csv")
RESULTS = Path("results")


def night(platform, day, snapshot, drop_states):
    """Kept general-election contests of one night, plus a count of every contest of that night by reason."""
    every = pd.read_csv(stretch.PRICES, dtype={"contest_id": str})
    every = every[(every["platform"] == platform) & (every["election_key"] == day)
                  & (every["country"] == "United States")]
    wrong = every[every["snapshot_utc"] != snapshot]
    if len(wrong):
        sys.exit(f"{platform}: {len(wrong)} contests of {day} are not priced at {snapshot}, e.g. "
                 f"{wrong['contest_id'].iloc[0]} at {wrong['snapshot_utc'].iloc[0]}. Fix the calendar first.")
    every = every.assign(reason=np.where(every["state"].isin(drop_states), "drop: runoff state", every["reason"]))
    every.loc[every["kind"] != "general", "reason"] = "drop: not a general election"
    kept = stretch.load(platform)
    kept = kept[(kept["election_key"] == day) & (kept["kind"] == "General") & ~kept["state"].isin(drop_states)]
    return kept.reset_index(drop=True), every["reason"].value_counts()


def level_verdict(low, high):
    if low > 1:
        return "underconfident"
    if high < 1:
        return "overconfident"
    return "no detectable miscalibration"


def change_verdict(low, high, baseline):
    if high < baseline:
        return "lower than 2024"
    if low > baseline:
        return "higher than 2024"
    return "consistent with 2024"


def reading(level, change):
    if level == "underconfident" and change in ("consistent with 2024", "higher than 2024"):
        return "Underconfidence persists in general elections: supports 'primaries are traded differently'."
    if level != "underconfident" and change == "lower than 2024":
        return "Underconfidence has not persisted: supports 'it switched off over time'."
    if level == "underconfident" and change == "lower than 2024":
        return "Still underconfident but less than in 2024: partial support for 'it switched off over time'."
    return "Inconclusive."


def run(day, snapshot, drop_states, stage):
    lines, rows = [], []
    lines.append(f"Midterm test, stage: {stage}. Run {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC on {day}.")
    for platform in ("Kalshi", "Polymarket"):
        test, reasons = night(platform, day, snapshot, drop_states)
        lines.append(f"\n{platform}: {len(test)} contests kept in {test['state'].nunique()} states")
        lines += [f"    {count:4d}  {reason}" for reason, count in reasons.items()]
        if len(test) < MIN_CONTESTS:
            lines.append(f"    fewer than {MIN_CONTESTS} contests: no test for this platform")
            rows.append({"platform": platform, "contests": len(test), "reading": "too few contests"})
            continue
        base, _ = night(platform, BASELINE_DAY, BASELINE_SNAPSHOT, set())
        base_b = stretch.fit(base["log_odds"], base["won"])
        if abs(base_b - BASELINE_B[platform]) > 1e-6:
            sys.exit(f"{platform}: the 2024 baseline recomputes to {base_b:.6f}, not the frozen "
                     f"{BASELINE_B[platform]:.6f}. The input data changed.")
        test_b = stretch.fit(test["log_odds"], test["won"])
        low, high = stretch.interval(stretch.bootstrap(test, "state")["All"])
        level, change = level_verdict(low, high), change_verdict(low, high, BASELINE_B[platform])
        rows.append({"platform": platform, "contests": len(test), "states": test["state"].nunique(), "b": test_b,
                     "low": low, "high": high, "level": level, "baseline_b": BASELINE_B[platform], "change": change,
                     "reading": reading(level, change)})
        show = lambda v: "no limit" if np.isinf(v) else f"{v:.3f}"
        lines += [f"    b = {show(test_b)}  (95%: {show(low)} to {show(high)})  -> {level}",
                  f"    against the 2024 value {BASELINE_B[platform]:.3f}  -> {change}",
                  f"    reading: {reading(level, change)}"]
    return pd.DataFrame(rows), lines


def unsettled():
    """Contests listed before the vote that are not in the price table yet, or None if there is no list."""
    if not ELIGIBLE.exists():
        return None
    listed = pd.read_csv(ELIGIBLE, dtype={"contest_id": str})
    listed = listed[~listed["state"].isin(RUNOFF_STATES)]
    priced = set(pd.read_csv(stretch.PRICES, dtype={"contest_id": str})["contest_id"])
    return listed[~listed["contest_id"].isin(priced)]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stage", choices=["preliminary", "final"])
    group.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    RESULTS.mkdir(exist_ok=True)

    if args.self_test:
        table, lines = run(BASELINE_DAY, BASELINE_SNAPSHOT, set(), "self-test on 5 November 2024")
        for _, row in table.iterrows():
            assert abs(row["b"] - BASELINE_B[row["platform"]]) < 1e-9 and row["change"] == "consistent with 2024"
        lines.append("\nSelf-test passed: the 2024 baseline is reproduced and reads as consistent with itself.")
        name = "midterm_test_selftest"
    else:
        today = datetime.now(timezone.utc).date()
        missing = unsettled()
        if args.stage == "preliminary" and today < PRELIMINARY_FROM:
            sys.exit(f"The preliminary run is fixed for {PRELIMINARY_FROM} or later.")
        if args.stage == "final" and today < FINAL_FROM and (missing is None or len(missing)):
            sys.exit(f"The final run waits until every eligible contest has settled or until {FINAL_FROM}.")
        table, lines = run(TEST_DAY, TEST_SNAPSHOT, RUNOFF_STATES, args.stage)
        if missing is None:
            lines.append("\nNo list of eligible contests from before the vote; unsettled contests not counted.")
        else:
            lines.append(f"\n{len(missing)} contests listed before the vote were not settled at this run.")
        if args.stage == "preliminary":
            lines.append("PRELIMINARY: the reading is judged on the final run only.")
        name = f"midterm_test_{args.stage}"
    table.to_csv(RESULTS / f"{name}.csv", index=False)
    (RESULTS / f"{name}.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
