"""The stretch model, shared by kalshi_fit.ipynb and polymarket_fit.ipynb.

One row per contest: the favourite's scaled price p and whether the favourite won. The model gives the
favourite the chance

    p^b / (p^b + (1 - p)^b)

b = 1: prices are calibrated; b above 1: the market is underconfident (favourites win more often than
their price); b below 1: overconfident. b is the value that gives the actual winners the highest chance
overall (maximum likelihood; a logistic regression on log(p / (1 - p)) with no intercept).
Needs numpy and pandas only.
"""

import numpy as np
import pandas as pd

PRICES = "../calendar_and_prices/contest_prices.csv"
SEED = 20261101
RESAMPLES = 2000


def load(platform, us_only=True):
    """Kept contests of one platform, one row per contest, from the favourite's side.

    The study is about US elections, so contests elsewhere are left out unless us_only=False.
    """
    df = pd.read_csv(PRICES, dtype={"contest_id": str})
    df = df[(df["platform"] == platform) & df["included"]].copy()
    if us_only:
        df = df[df["country"] == "United States"]
    df["state"] = df["state"].fillna("none")
    a_favourite = df["p_a"] >= df["p_b"]
    df["price"] = np.where(a_favourite, df["p_a"], df["p_b"])
    df["favourite"] = np.where(a_favourite, df["candidate_a"], df["candidate_b"])
    df["won"] = np.where(a_favourite, df["a_won"], 1 - df["a_won"]).astype(int)
    df["kind"] = df["kind"].str.capitalize()
    df["log_odds"] = np.log(df["price"] / (1 - df["price"]))
    return df.reset_index(drop=True)


def stretch(price, b):
    """The model's chance for an option priced `price`, given b."""
    price = np.asarray(price, dtype=float)
    return 1 / (1 + ((1 - price) / price) ** b)


def fit(log_odds, won, b_max=50.0):
    """Maximum-likelihood b (Newton's method). Returns inf when every favourite won, or every one lost."""
    x, y = np.asarray(log_odds, float), np.asarray(won, float)
    if len(x) == 0:
        return np.nan
    informative = x != 0  # a 50/50 contest says nothing about b
    if np.all(y[informative] == 1):  # every favourite won: the likelihood keeps rising as b grows
        return np.inf
    if np.all(y[informative] == 0):
        return -np.inf
    b = 1.0
    for _ in range(100):
        chance = 1 / (1 + np.exp(-b * x))
        slope = np.sum(x * (y - chance))
        curvature = np.sum(x * x * chance * (1 - chance))
        if curvature <= 0:
            break
        step = slope / curvature
        b = float(np.clip(b + step, -b_max, b_max))
        if abs(step) < 1e-10:
            break
    return b


def log_loss(chance, won):
    """Mean of minus the log of the chance given to what actually happened (lower is better)."""
    chance, won = np.asarray(chance, float), np.asarray(won, float)
    return float(np.mean(-np.log(np.where(won == 1, chance, 1 - chance))))


def brier(chance, won):
    """Mean squared gap between the chance and what happened (lower is better)."""
    return float(np.mean((np.asarray(chance, float) - np.asarray(won, float)) ** 2))


def bootstrap(df, by, groups=None, resamples=RESAMPLES, seed=SEED):
    """b refitted on resampled data. `by` is "day" (whole election days), "state" (every contest in a
    state together; for a single election day, where days cannot be resampled) or "contest".

    Returns a DataFrame with one column per group ("All", plus each value of `groups`), one row per resample.
    """
    rng = np.random.default_rng(seed)
    units = {"day": df["election_key"], "state": df["state"], "contest": pd.Series(df.index, index=df.index)}[by]
    rows_of = {unit: np.flatnonzero(units.to_numpy() == unit) for unit in units.unique()}
    keys = list(rows_of)
    names = ["All"] + (sorted(df[groups].unique()) if groups else [])
    out = np.empty((resamples, len(names)))
    for i in range(resamples):
        rows = np.concatenate([rows_of[keys[j]] for j in rng.integers(0, len(keys), len(keys))])
        sample = df.iloc[rows]
        out[i, 0] = fit(sample["log_odds"], sample["won"])
        for k, name in enumerate(names[1:], start=1):
            part = sample[sample[groups] == name]
            out[i, k] = fit(part["log_odds"], part["won"]) if len(part) >= 5 else np.nan
    return pd.DataFrame(out, columns=names)


def interval(values, level=0.95):
    """Middle 95% of resampled b. An end is infinite when enough resamples had every favourite win."""
    values = np.asarray(values, float)
    values = np.clip(values[~np.isnan(values)], -1e6, 1e6)
    ends = np.quantile(values, [(1 - level) / 2, 1 - (1 - level) / 2])
    return tuple(float(np.sign(e) * np.inf) if abs(e) >= 1e6 else float(e) for e in ends)


def leave_one_day_out(df):
    """Fit b without each election day, then score that day's contests with the stretch and the raw price."""
    rows = []
    for day, held in df.groupby("election_key"):
        rest = df[df["election_key"] != day]
        b = fit(rest["log_odds"], rest["won"])
        chance = stretch(held["price"], b)
        rows.append({
            "election_key": day, "contests": len(held), "b_without_day": b,
            "log_loss_raw": log_loss(held["price"], held["won"]), "log_loss_stretched": log_loss(chance, held["won"]),
            "brier_raw": brier(held["price"], held["won"]), "brier_stretched": brier(chance, held["won"]),
        })
    out = pd.DataFrame(rows)
    out["log_loss_gain"] = out["log_loss_raw"] - out["log_loss_stretched"]  # above 0: the stretch helped
    return out


def wilson(wins, n, z=1.96):
    """95% interval for a share, which stays inside 0-1 even for small groups."""
    share = wins / n
    centre = (share + z * z / (2 * n)) / (1 + z * z / n)
    half = z * np.sqrt(share * (1 - share) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return centre - half, centre + half
