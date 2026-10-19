# Data

The raw data come from a SQLite database provided during an internship at a
Taiwan futures brokerage. The data are proprietary and are **not included** in
this repository.

To run the scripts, place the database at `data/futures.db`. Everything in
`data/` except this README is ignored by git, so the database cannot be
committed by accident.

## Tables

| Table | Instrument | Frequency | Period | Date / time format | Columns used |
|---|---|---|---|---|---|
| `ES` | E-mini S&P 500 futures | 1-minute | 2020-01-02 to 2025-10-30 | `2020/1/2` + `07:01:00` | `Date`, `Time`, `Close` (about 2.06M rows) |
| `GC` | Gold futures | 1-minute | 2020-01-01 to 2025-10-30 | same as ES | `Date`, `Time`, `Close` (about 2.0M rows) |
| `VIX` | CBOE Volatility Index | daily | 2019-01-02 to 2025-10-30 | `10/30/2025` (month/day/year) | `Date`, `Price` |
| `HG` | Copper futures | daily | 2020-01-01 to 2025-10-30 | `2025/10/30` | `Date`, `close_price` |
| `BTC` | Bitcoin | daily, incl. weekends | 2020-01-01 to 2025-10-30 | `2025/10/30` | `date`, `close` (with thousands separators, e.g. `108,320.10`) |
| `USbond` | US 10-year Treasury yield | daily | - | `10/30/2025` | not used |

## Data issues and how they are handled

The findings below come from `src/data_checks.py`, which only reads and prints.
The cleaning itself is done in `src/regime_backtest.py`.

1. **Time zone.** CME futures stop trading every day from 16:00 to 17:00
   Chicago time (CT), so the hour with the fewest bars reveals the clock of
   each series.
   - GC is in CT for the whole sample.
   - ES is stored in Asia/Taipei time until the weekend of 2024-10-12/13
     (last Taipei-time bar: 2024-10-12 05:00 = Friday 16:00 CDT; first CT bar:
     2024-10-13 17:01) and in CT afterwards.
   - Fix: ES bars before 2024-10-13 are converted from Asia/Taipei to
     America/Chicago, with daylight saving time handled.
2. **Trading day.** Bars from 17:00 CT onward belong to the next CME trading
   day. The daily close is the last bar of the trading day (16:00 CT).
3. **Artifact and partial days are dropped**: days with fewer than 60 bars,
   days whose last bar is before 11:00 CT, and weekend dates.
   - ES: 5 days dropped (2021-04-02 and 2023-04-07 Good Friday early close,
     2025-01-09 national day of mourning early close, 2023-12-25 single bar,
     2025-10-30 half day). Result: 1,504 trading days.
   - GC: 3 days dropped (2023-04-07 single bar, 2024-07-29 half day before a
     data gap, 2025-10-30 half day). Result: 1,459 trading days.
4. **Gaps.** GC has no data from 2024-07-29 04:20 to 2024-09-30 17:01
   (63 days); ES has no gaps. A daily return that spans more than 7 calendar
   days is set to missing and treated as 0 (one GC return). During the GC gap
   the copper/gold signal keeps its last known value.
5. **Contract rolls.** No visible price jumps at ES roll dates.
6. **Calendar.** The master calendar is the ES trading days. VIX (missing on
   11 ES trading days, US holidays when futures still trade) and HG (missing
   on 3 days) are forward-filled. BTC trades every calendar day and is matched
   by calendar date.
7. **Close times differ.** VIX closes at about 15:15 CT, ES and GC at 16:00 CT,
   BTC at 00:00 UTC (evening in Chicago, after the ES close). BTC information
   is therefore used one day later than the other signals.

## Alignment check

Stocks fall on days when VIX jumps. After the time-zone fix, the correlation
between the ES daily return and the VIX daily change is -0.712 on the same
day, versus +0.096 and +0.052 when VIX is shifted by one day in either
direction. This confirms that ES and VIX are on the same clock.
