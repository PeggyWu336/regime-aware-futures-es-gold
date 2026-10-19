# ============================================================
# data_checks.py
#
# Data audit for the regime strategy. Run this BEFORE regime_backtest.py.
# It does not change any data; it only prints what the raw data look like
# and documents where the cleaning rules in regime_backtest.py come from:
#
#   Check 1  Inventory     - which tables exist, row counts, first/last rows
#   Check 2  Time zone     - the daily trading halt (16:00-17:00 Chicago time)
#                            reveals each series' clock; finds the date when
#                            the ES timestamps switch from Taipei to Chicago time
#   Check 3  Gaps          - calendar gaps longer than 4 days in minute data
#   Check 4  Daily bars    - trading days, partial days, single-bar artifacts,
#                            largest daily moves (contract-roll jumps?)
#   Check 5  Daily series  - VIX / copper / BTC date ranges and duplicates
#   Check 6  Alignment     - after the fix, ES returns vs VIX changes
#                            should be most negative on the same day
#
# Run from the repository root:  python src/data_checks.py
# The database is expected at data/futures.db (not included, see data/README.md).
# ============================================================

import os
import sqlite3
import numpy as np
import pandas as pd

# ------------------------------------------------------------
# Settings
# ------------------------------------------------------------
# The database is proprietary and is NOT included in the repository.
DB_PATH = "data/futures.db"   # relative to the repository root

RAW_TABLES   = ["ES", "GC", "VIX", "HG", "BTC", "USbond"]
GAP_DAYS     = 4                           # report minute-data gaps longer than this
ES_TZ_SWITCH = pd.Timestamp("2024-10-13")  # result of Check 2, used in Check 4 and 6

pd.set_option("display.width", 200)
pd.set_option("display.max_rows", 200)

def header(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)

if not os.path.exists(DB_PATH):
    raise FileNotFoundError(f"{DB_PATH} not found. Run from the repository root; see data/README.md.")
conn = sqlite3.connect(DB_PATH)
existing = set(pd.read_sql("SELECT name FROM sqlite_master WHERE type='table'", conn)["name"])


# ------------------------------------------------------------
# Check 1: inventory of the raw tables
# ------------------------------------------------------------
header("CHECK 1  Inventory of raw tables")
print(f"Tables in the database: {len(existing)}")
for t in RAW_TABLES:
    if t not in existing:
        print(f"\n[{t}] not found")
        continue
    n = pd.read_sql(f'SELECT COUNT(*) AS n FROM "{t}"', conn)["n"][0]
    print(f"\n[{t}] rows: {n:,}")
    print("first 2 rows:")
    print(pd.read_sql(f'SELECT * FROM "{t}" LIMIT 2', conn).to_string(index=False))
    print("last 2 rows:")
    print(pd.read_sql(f'SELECT * FROM "{t}" ORDER BY rowid DESC LIMIT 2', conn).to_string(index=False))


# ------------------------------------------------------------
# Load minute data (raw timestamps, no conversion yet)
# ------------------------------------------------------------
def load_minute_raw(table):
    df = pd.read_sql(f'SELECT Date, Time, Close FROM "{table}"', conn)
    ts = pd.to_datetime(df["Date"] + " " + df["Time"], format="%Y/%m/%d %H:%M:%S")
    return pd.DataFrame({"ts": ts, "close": df["Close"].astype(float).values}) \
             .sort_values("ts").reset_index(drop=True)

es_raw = load_minute_raw("ES")
gc_raw = load_minute_raw("GC")


# ------------------------------------------------------------
# Check 2: which clock is each series on?
# ------------------------------------------------------------
# CME futures stop trading every day from 16:00 to 17:00 Chicago time.
# The hour with the fewest bars therefore tells us the time zone:
#   hour 16       -> Chicago time
#   hour 5 or 6   -> Taipei time (16:00 CT = 05:00 Taipei in summer, 06:00 in winter)
header("CHECK 2  Time zone: hour of the daily trading halt")

def halt_hour_by_day(df):
    """For each Tue-Thu calendar day (full sessions), the hour with the fewest bars."""
    x = df[df["ts"].dt.dayofweek.isin([1, 2, 3])]
    cnt = (x.groupby([x["ts"].dt.normalize(), x["ts"].dt.hour]).size()
             .unstack(fill_value=0)
             .reindex(columns=range(24), fill_value=0))
    return cnt.idxmin(axis=1)

for name, df in [("ES", es_raw), ("GC", gc_raw)]:
    hh = halt_hour_by_day(df)
    print(f"\n{name}: bars per hour of day (raw timestamps)")
    print(df["ts"].dt.hour.value_counts().sort_index().to_string())
    print(f"\n{name}: halt hour by month  {{hour: number of days}}")
    print(hh.groupby(hh.index.to_period("M")).agg(lambda s: dict(s.value_counts())).to_string())

es_hh = halt_hour_by_day(es_raw)
last_taipei = es_hh[es_hh.isin([5, 6])].index.max()
first_chicago = es_hh[es_hh == 16].index.min()
print(f"\nES last day with the Taipei-time pattern (halt at 5/6):  {last_taipei.date()}")
print(f"ES first day with the Chicago-time pattern (halt at 16):   {first_chicago.date()}")

# Look at the raw bars around the switch to find the exact cut
around = es_raw[(es_raw["ts"] >= last_taipei - pd.Timedelta(days=2)) &
                (es_raw["ts"] <= first_chicago + pd.Timedelta(days=3))]
print("\nES bars per calendar day around the switch:")
print(around.groupby(around["ts"].dt.normalize())["ts"]
            .agg(n="size", first="min", last="max").to_string())
print(f"\n-> Setting used in the backtest: ES_TZ_SWITCH = {ES_TZ_SWITCH.date()}")


# ------------------------------------------------------------
# Check 3: gaps in the minute data
# ------------------------------------------------------------
header(f"CHECK 3  Gaps longer than {GAP_DAYS} days in minute data")
for name, df in [("ES", es_raw), ("GC", gc_raw)]:
    t = df["ts"]
    jumps = t.diff() > pd.Timedelta(days=GAP_DAYS)
    print(f"\n{name}: {int(jumps.sum())} gap(s)")
    for i in t.index[jumps]:
        print(f"  {t[i - 1]}  ->  {t[i]}   ({(t[i] - t[i - 1]).days} days)")


# ------------------------------------------------------------
# Check 4: daily bars after putting ES on Chicago time
# ------------------------------------------------------------
header("CHECK 4  Daily bars (Chicago time, CME trading day)")

def to_chicago(df, taipei_before=None):
    out = df.copy()
    if taipei_before is not None:
        m = out["ts"] < taipei_before
        out.loc[m, "ts"] = (out.loc[m, "ts"].dt.tz_localize("Asia/Taipei")
                                             .dt.tz_convert("America/Chicago")
                                             .dt.tz_localize(None))
    out = out.sort_values("ts")
    # bars from 17:00 CT onward belong to the next trading day
    out["trade_day"] = out["ts"].dt.normalize() + pd.to_timedelta(
        (out["ts"].dt.hour >= 17).astype(int), unit="D")
    return out

def daily_summary(name, df):
    d = df.groupby("trade_day").agg(close=("close", "last"),
                                    last_ts=("ts", "last"),
                                    n_bars=("close", "size"))
    print(f"\n{name}: {len(d)} trade days ({d.index.min().date()} to {d.index.max().date()})")
    print("  weekday counts (0=Mon):", d.index.dayofweek.value_counts().sort_index().to_dict())
    print("  most common last-bar times:",
          d["last_ts"].dt.strftime("%H:%M").value_counts().head(5).to_dict())
    print("  bars per day: min / median / max =",
          d["n_bars"].min(), "/", int(d["n_bars"].median()), "/", d["n_bars"].max())
    odd = d[(d["n_bars"] < 60) | (d["last_ts"].dt.hour < 11) | (d.index.dayofweek >= 5)]
    print(f"  artifact or partial days (dropped in the backtest): {len(odd)}")
    if len(odd):
        print(odd[["last_ts", "n_bars"]].to_string())
    d["ret"] = d["close"].pct_change()
    print("  10 largest absolute daily returns (a regular jump at roll dates would")
    print("  indicate an unadjusted continuous contract):")
    top = d["ret"].abs().sort_values(ascending=False).head(10).index
    print(d.loc[top, ["close", "ret"]].to_string())
    return d

es_ct = to_chicago(es_raw, taipei_before=ES_TZ_SWITCH)
gc_ct = to_chicago(gc_raw)
print("\nES halt hour after conversion (should be 16 in every year):")
hh_after = halt_hour_by_day(es_ct)
print(hh_after.groupby(hh_after.index.to_period("Y")).agg(lambda s: dict(s.value_counts())).to_string())
es_d = daily_summary("ES", es_ct)
gc_d = daily_summary("GC", gc_ct)


# ------------------------------------------------------------
# Check 5: daily series (VIX, copper, BTC)
# ------------------------------------------------------------
header("CHECK 5  Daily series")
vix = pd.read_sql('SELECT Date, Price FROM "VIX"', conn)
vix = pd.Series(vix["Price"].astype(float).values,
                index=pd.to_datetime(vix["Date"], format="%m/%d/%Y")).sort_index()
hg = pd.read_sql('SELECT Date, close_price FROM "HG"', conn)
hg = pd.Series(hg["close_price"].astype(float).values,
               index=pd.to_datetime(hg["Date"], format="%Y/%m/%d")).sort_index()
btc = pd.read_sql('SELECT date, close FROM "BTC"', conn)
btc = pd.Series(btc["close"].astype(str).str.replace(",", "").astype(float).values,
                index=pd.to_datetime(btc["date"], format="%Y/%m/%d")).sort_index()
conn.close()

for name, s in [("VIX", vix), ("HG (copper)", hg), ("BTC", btc)]:
    print(f"{name}: {len(s)} rows, {s.index.min().date()} to {s.index.max().date()}, "
          f"duplicated dates: {s.index.duplicated().sum()}, missing values: {int(s.isna().sum())}")

# Coverage on ES trading days (normal trading days only)
es_days = es_d[(es_d["n_bars"] >= 60) & (es_d["last_ts"].dt.hour >= 11)
               & (es_d.index.dayofweek < 5)].index
print("\nCoverage on ES trading days:")
for name, idx in [("GC", gc_d.index), ("VIX", vix.index), ("HG", hg.index), ("BTC", btc.index)]:
    missing = es_days.difference(idx)
    print(f"  {name}: missing on {len(missing)} of {len(es_days)} ES days; "
          f"first few: {[x.date().isoformat() for x in missing[:6]]}")


# ------------------------------------------------------------
# Check 6: time alignment after the fix
# ------------------------------------------------------------
# Stocks fall on days when VIX jumps. If ES and VIX are on the same clock,
# the correlation is strongly negative on the same day and close to 0 when
# VIX is shifted by one day.
header("CHECK 6  Alignment: corr(ES daily return, VIX daily change)")
es_ret = es_d.loc[es_days, "ret"]
vix_chg = vix.pct_change().reindex(es_days)
for k in [-1, 0, 1]:
    print(f"  VIX shifted {k:+d} day: {es_ret.corr(vix_chg.shift(k)):+.3f}")
print("  Expect the most negative value at shift 0.")
