# Calibration of US election markets on Kalshi and Polymarket: frozen before the 2026 midterms

Nikita Kirillov, Maastricht University

Archived before the vote: code and frozen results [10.5281/zenodo.23776845](https://doi.org/10.5281/zenodo.23776845) (release `v1.0-preregistration`); raw pulls in `pull/data` [10.5281/zenodo.23776979](https://doi.org/10.5281/zenodo.23776979).

We pulled every settled election market on Kalshi and Polymarket, kept contests with exactly two candidates and one
clear winner, and priced each one at a fixed moment before the vote ended: 17:00 in the easternmost US time zone voting
that day. For each platform we then fitted one number, the stretch `b`, which turns the favourite's price `p` into the
chance `p^b / (p^b + (1 - p)^b)`. `b` = 1 means prices are calibrated, above 1 means underconfident (favourites win more
often than their price says), below 1 overconfident. The numbers below use US contests only and are frozen before the
midterms of 3 November 2026, whose contests will be priced the same way at 22:00 UTC on 3 November and compared with
the 5 November 2024 baseline. Intervals are 95%, from 2,000 resamples (seed 20261101); "no limit" means enough
resamples had every favourite win that `b` has no upper bound.

## Baseline: general elections on 5 November 2024

| | Contests | States | `b` | Interval (states resampled) |
|---|---|---|---|---|
| Kalshi | 60 | 29 | **1.97** | 1.27 to 3.77 |
| Polymarket | 35 | 26 | **1.83** | 0.99 to no limit |

## All US training contests (November 2023 to September 2026)

| | Contests | Election days | `b` | Interval (days resampled) | Interval (states resampled) |
|---|---|---|---|---|---|
| Kalshi, all | 152 | 26 | 0.58 | 0.14 to 1.03 | 0.39 to 0.86 |
| Kalshi, general | 74 | 6 | 1.28 | -0.77 to no limit | 0.83 to 2.62 |
| Kalshi, primary | 78 | 21 | 0.32 | 0.03 to 0.70 | 0.10 to 0.59 |
| Polymarket, all | 64 | 23 | 1.23 | 0.58 to 1.75 | 0.75 to 2.19 |
| Polymarket, general | 41 | 7 | 1.61 | 0.33 to no limit | 0.80 to no limit |
| Polymarket, primary | 23 | 17 | 0.88 | 0.40 to 1.67 | 0.40 to 1.56 |

## US only against all countries

| `b` | Kalshi, US | Kalshi, all countries | Polymarket, US | Polymarket, all countries |
|---|---|---|---|---|
| All | 0.58 (152) | 0.62 (169) | 1.23 (64) | 1.05 (84) |
| General | 1.28 (74) | 1.29 (91) | 1.61 (41) | 1.15 (61) |
| Primary | 0.32 (78) | 0.32 (78) | 0.88 (23) | 0.88 (23) |

Contests in brackets. Full tables are in `fit/results/`; every file's SHA-256 is in `SHA256SUMS`.
