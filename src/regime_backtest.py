# ============================================================
# regime_backtest.py
#
# Regime-aware ES / Gold futures strategy
#   - Risk regime: VIX (fear gauge) x Copper/Gold ratio (growth vs. safety)
#     combined into a 2x2 grid of states A / B / C / D
#   - Strategy: 60-day trend following on E-mini S&P 500 futures (ES),
#     optionally combined with gold futures (GC)
#   - Extra check: does a recent Bitcoin crash mark the most dangerous
#     days inside the worst regime (D vs D+)?
#
# Design choices (see data/README.md for the data itself)
#   1. One clock: all futures timestamps are in Chicago time (CT).
#      ES bars before 2024-10-13 are stored in Taipei time and converted.
#   2. No look-ahead: a signal known at today's close is traded at today's
#      close and earns tomorrow's return. BTC closes at 00:00 UTC, which is
#      after the ES close, so BTC information waits one extra day.
#   3. Thresholds use only past data (rolling 1-year VIX median,
#      rolling 3-year Cu/Au z-score).
#   4. Stop-loss: the loss on the day the stop is hit is kept; the strategy
#      stays flat until the trend signal changes direction.
#   5. Simple returns, compounded wealth, 2 bps cost per unit of turnover.
#   6. GC has no data from 2024-07-29 to 2024-09-30; gold return is 0 there.
#
# Output: tables (CSV) and figures (PNG) in OUT_DIR.
# Run from the repository root:  python src/regime_backtest.py
# The database is expected at data/futures.db (not included, see data/README.md).
# ============================================================

import os
import sqlite3
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# ------------------------------------------------------------
# 0) Settings
# ------------------------------------------------------------
# Paths are relative to the repository root.
# The database is proprietary and is NOT included in the repository.
DB_PATH = "data/futures.db"
OUT_DIR = "results"

# --- Data cleaning ---
ES_TZ_SWITCH  = pd.Timestamp("2024-10-13")  # ES bars before this date are in Asia/Taipei time
EVAL_START    = pd.Timestamp("2021-01-01")  # common start (Cu/Au z-score needs >= 1 year of history)
MIN_BARS      = 60                          # a "day" with fewer minute bars is a data artifact
MIN_LAST_HOUR = 11                          # a day whose last bar is before 11:00 CT is a partial day
MAX_GAP_DAYS  = 7                           # a return spanning a longer calendar gap is set to missing

# --- Strategy parameters (kept from the original research design) ---
MOM_WIN    = 60                   # trend lookback in trading days
STOP_LOSS  = -0.05                # per-trade stop-loss (-5%)
VIX_WIN, VIX_MIN   = 252, 126     # rolling window (and minimum history) for the VIX median
CUAU_WIN, CUAU_MIN = 756, 252     # rolling window (3 years, minimum 1 year) for the Cu/Au z-score
CUAU_TH    = -0.5                 # Cu/Au z-score below this = "risk-off"
BTC_CRASH  = np.log(0.95)         # BTC crash day: price falls 5% or more in one day
BTC_WINDOW = "10D"                # BTC stress = at least one crash in the last 10 calendar days
COST_BPS   = 2.0                  # trading cost per unit of exposure changed (basis points)

os.makedirs(OUT_DIR, exist_ok=True)
pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)


# ------------------------------------------------------------
# 1) Load data
# ------------------------------------------------------------
if not os.path.exists(DB_PATH):
    raise FileNotFoundError(f"{DB_PATH} not found. Run from the repository root; see data/README.md.")
conn = sqlite3.connect(DB_PATH)

def load_minute(table, taipei_before=None):
    """Read minute bars, put them on Chicago time and assign each bar to a CME trading day.

    taipei_before: bars with a timestamp before this date are in Taipei time
                   and are converted to Chicago time (daylight saving handled).
    """
    df = pd.read_sql(f'SELECT Date, Time, Close FROM "{table}"', conn)
    ts = pd.to_datetime(df["Date"] + " " + df["Time"], format="%Y/%m/%d %H:%M:%S")
    out = pd.DataFrame({"ts": ts, "close": df["Close"].astype(float).values})
    if taipei_before is not None:
        m = out["ts"] < taipei_before
        out.loc[m, "ts"] = (out.loc[m, "ts"].dt.tz_localize("Asia/Taipei")
                                             .dt.tz_convert("America/Chicago")
                                             .dt.tz_localize(None))
    out = out.sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)
    # CME session runs 17:00 CT (previous evening) to 16:00 CT,
    # so bars from 17:00 CT onward belong to the next trading day.
    out["trade_day"] = out["ts"].dt.normalize() + pd.to_timedelta(
        (out["ts"].dt.hour >= 17).astype(int), unit="D")
    return out

def to_daily(m, name):
    """Collapse minute bars into daily closes and daily simple returns."""
    d = m.groupby("trade_day").agg(close=("close", "last"),     # last bar = daily close
                                   last_ts=("ts", "last"),
                                   n_bars=("close", "size"))
    # Keep only real trading days: enough bars, a normal end time, Monday-Friday
    keep = ((d["n_bars"] >= MIN_BARS)
            & (d["last_ts"].dt.hour >= MIN_LAST_HOUR)
            & (d.index.dayofweek < 5))
    dropped = d[~keep]
    d = d[keep].copy()
    # A return that spans a long data gap is not a one-day return -> missing
    gap = d.index.to_series().diff().dt.days
    d["ret"] = d["close"].pct_change()
    d.loc[gap > MAX_GAP_DAYS, "ret"] = np.nan
    print(f"{name}: {len(d)} trading days ({d.index.min().date()} to {d.index.max().date()}), "
          f"dropped {len(dropped)} artifact/partial days, "
          f"returns set missing after gaps: {int((gap > MAX_GAP_DAYS).sum())}")
    print(f"  last-bar time (CT) most common: "
          f"{d['last_ts'].dt.strftime('%H:%M').value_counts().head(3).to_dict()}")
    return d

print("=" * 70)
print("1) DATA")
es = to_daily(load_minute("ES", taipei_before=ES_TZ_SWITCH), "ES")
gc = to_daily(load_minute("GC"), "GC")

# Daily series (each table has its own date format)
vix = pd.read_sql('SELECT Date, Price FROM "VIX"', conn)
vix = pd.Series(vix["Price"].astype(float).values,
                index=pd.to_datetime(vix["Date"], format="%m/%d/%Y")).sort_index()
hg = pd.read_sql('SELECT Date, close_price FROM "HG"', conn)
hg = pd.Series(hg["close_price"].astype(float).values,
               index=pd.to_datetime(hg["Date"], format="%Y/%m/%d")).sort_index()
btc = pd.read_sql('SELECT date, close FROM "BTC"', conn)
btc = pd.Series(btc["close"].astype(str).str.replace(",", "").astype(float).values,  # "108,320.10"
                index=pd.to_datetime(btc["date"], format="%Y/%m/%d")).sort_index()
conn.close()

cal = es.index                                   # master calendar = ES trading days
es_ret = es["ret"]
gc_ret = gc["ret"].reindex(cal)                  # days without GC data -> NaN (treated as 0)

# Sanity check of the time alignment: stocks fall when VIX jumps, so the
# same-day correlation should be strongly negative and the +/-1 day values near 0.
vix_chg = vix.pct_change().reindex(cal)
print("\nAlignment check  corr(ES return, VIX change):")
for k in [-1, 0, 1]:
    print(f"  VIX shifted {k:+d} day: {es_ret.corr(vix_chg.shift(k)):+.3f}")
print("  (expect the most negative value at shift 0)")


# ------------------------------------------------------------
# 2) Regime signals (each value is known at that day's close)
# ------------------------------------------------------------
# VIX: "high" when today's VIX is at or above its median of the past year
vix_med = vix.rolling(VIX_WIN, min_periods=VIX_MIN).median()
vix_high_native = (vix >= vix_med).astype(float).where(vix_med.notna())

# Copper/Gold ratio: copper rises with growth, gold with fear.
# A low ratio relative to its own history (z-score) = weak risk appetite.
# Mean and std use the previous 3 years only (today excluded).
cuau = (hg.reindex(gc.index) / gc["close"]).dropna()
mu = cuau.rolling(CUAU_WIN, min_periods=CUAU_MIN).mean().shift(1)
sd = cuau.rolling(CUAU_WIN, min_periods=CUAU_MIN).std(ddof=0).shift(1)
cuau_z = (cuau - mu) / sd
cuau_off_native = (cuau_z < CUAU_TH).astype(float).where(cuau_z.notna())

# Put everything on the ES calendar; carry the last known value forward
# (e.g. US holidays for VIX, the 2024 GC data gap for Cu/Au)
state = pd.DataFrame(index=cal)
state["vix"] = vix.reindex(cal, method="ffill")
state["vix_med"] = vix_med.reindex(cal, method="ffill")
state["vix_high"] = vix_high_native.reindex(cal, method="ffill")
state["cuau_z"] = cuau_z.reindex(cal, method="ffill")
state["cuau_off"] = cuau_off_native.reindex(cal, method="ffill")

# 2x2 regime: A = calm, B = growth worry, C = market fear, D = both (worst case)
state["D"] = ((state["vix_high"] == 1) & (state["cuau_off"] == 1)).astype(float)
state.loc[state[["vix_high", "cuau_off"]].isna().any(axis=1), "D"] = np.nan
labels = {(0, 0): "A", (0, 1): "B", (1, 0): "C", (1, 1): "D"}
state["regime"] = [labels.get((int(v), int(c))) if pd.notna(v) and pd.notna(c) else None
                   for v, c in zip(state["vix_high"], state["cuau_off"])]

# Bitcoin: crash day, then "stress" if a crash happened in the last 10 calendar days
btc_lr = np.log(btc / btc.shift(1))
btc_crash = (btc_lr <= BTC_CRASH).astype(float)
btc_off = btc_crash.rolling(BTC_WINDOW).max()

# Evaluation starts once every signal has enough history
first_valid = state["D"].first_valid_index()
eval_start = max(EVAL_START, cal[cal.get_loc(first_valid) + 2])
eval_end = cal.max()
print(f"\nFirst day with a full regime state: {first_valid.date()}")
print(f"Evaluation period: {eval_start.date()} to {eval_end.date()}")


# ------------------------------------------------------------
# 3) Strategy engine
# ------------------------------------------------------------
def lagged_states(extra_lag=0):
    """Regime information that is actually available when the position for day t is set.

    The position for day t is decided at the close of day t-1, so day t uses
    the state of day t-1 (or earlier when extra_lag > 0, for robustness tests).
    """
    k = 1 + extra_lag
    s = pd.DataFrame(index=cal)
    s["vix_high"] = state["vix_high"].shift(k).fillna(0)
    s["D"] = state["D"].shift(k).fillna(0)
    s["regime"] = state["regime"].shift(k)
    # BTC's daily close is at 00:00 UTC = evening in Chicago, i.e. after the ES close.
    # At the ES close of decision day p, the latest finished BTC day is p-1.
    decide_day = pd.Series(cal, index=cal).shift(k)
    known_btc_day = decide_day - pd.Timedelta(days=1)
    vals = np.full(len(cal), np.nan)
    ok = known_btc_day.notna().values
    vals[ok] = btc_off.reindex(pd.DatetimeIndex(known_btc_day[ok]), method="ffill").values
    s["btc_off"] = np.nan_to_num(vals)
    s["D_plus"] = ((s["D"] == 1) & (s["btc_off"] == 1)).astype(float)
    return s

def trend_positions(extra_lag=0):
    """ES trend positions: +1 long, -1 short, 0 flat.

    Rules
      - Long if ES is higher than 60 trading days ago, short if lower
        (signal from yesterday's close).
      - Flat on the first trading day of each quarter (quarterly contract roll).
      - Stop-loss: if a trade loses 5% or more (checked at the close), exit;
        that day's loss counts. Stay flat until the trend signal flips.
    Returns the position series and the list of stop-loss dates.
    """
    close = es["close"]
    sig_raw = np.sign(close / close.shift(MOM_WIN) - 1)
    sig = sig_raw.shift(1 + extra_lag).fillna(0).values
    q = cal.to_period("Q")
    q_first = np.r_[False, q[1:] != q[:-1]]       # first trading day of a new quarter
    r = es_ret.fillna(0).values

    pos = np.zeros(len(cal))
    stops = []
    prev, cum, stopped_dir = 0.0, 0.0, 0.0        # cum = running return of the current trade
    for t in range(len(cal)):
        target = sig[t]
        if stopped_dir != 0:
            if target == stopped_dir:
                target = 0.0              # stopped out: wait for the trend to flip
            else:
                stopped_dir = 0.0         # trend flipped: the stop is cleared
        if q_first[t]:
            target = 0.0                  # quarterly roll day: flat
        if target != prev:
            cum = 0.0                     # a new trade starts
        pos[t] = target
        if target != 0:
            cum = (1 + cum) * (1 + target * r[t]) - 1
            if cum <= STOP_LOSS:          # stop hit at today's close (today's loss is kept)
                stops.append(cal[t])
                stopped_dir = target
        prev = target
    return pd.Series(pos, index=cal), stops

def run_strategy(exp_es, exp_gc):
    """Daily returns of a portfolio given its ES and gold exposures (weights).

    gross    = ES weight x ES return + gold weight x gold return
    turnover = absolute change in weights (what has to be traded)
    net      = gross - turnover x cost
    """
    exp_es = exp_es.reindex(cal).fillna(0)
    exp_gc = exp_gc.reindex(cal).fillna(0)
    gross = exp_es * es_ret.fillna(0) + exp_gc * gc_ret.fillna(0)
    turnover = exp_es.diff().abs().fillna(exp_es.abs()) + exp_gc.diff().abs().fillna(exp_gc.abs())
    net = gross - turnover * COST_BPS / 1e4
    return pd.DataFrame({"gross": gross, "net": net, "turnover": turnover,
                         "exp_es": exp_es, "exp_gc": exp_gc})

def build_all(extra_lag=0):
    """Build every strategy as (ES weight, gold weight) and run it."""
    s = lagged_states(extra_lag)
    pos, stops = trend_positions(extra_lag)
    one, zero = pd.Series(1.0, index=cal), pd.Series(0.0, index=cal)
    strategies = {
        # Passive benchmarks
        "ES Buy & Hold": (one, zero),
        "Gold Buy & Hold": (zero, one),
        "50/50 ES + Gold Buy & Hold": (0.5 * one, 0.5 * one),
        # Trend strategy and its regime versions
        "ES Trend (baseline)": (pos, zero),
        "ES Trend + Regime filter": (pos * (1 - s["D"]), zero),            # flat in regime D
        "ES Trend + Gold 50/50 (no switch)": (0.5 * pos, 0.5 * one),         # always half gold
        "ES Trend + Gold switch": (0.5 * pos * (1 - s["D"]), 0.5 + 0.5 * s["D"]),  # 100% gold in D
        # Alternative: ES flat whenever VIX is high, gold flat in D, 50% each
        "Alt: VIX-filtered ES + Gold 50/50": (0.5 * pos * (1 - s["vix_high"]), 0.5 * (1 - s["D"])),
    }
    res = {k: run_strategy(*v) for k, v in strategies.items()}
    return res, s, pos, stops


# ------------------------------------------------------------
# 4) Metrics
# ------------------------------------------------------------
def perf(r):
    """CAGR, annualized volatility, Sharpe ratio (risk-free = 0) and max drawdown."""
    r = r.dropna()
    n = len(r)
    if n < 2 or r.std() == 0:
        return dict(days=n, CAGR=np.nan, vol=np.nan, Sharpe=np.nan, MDD=np.nan)
    wealth = (1 + r).cumprod()
    mdd = (wealth / wealth.cummax() - 1).min()
    return dict(days=n,
                CAGR=wealth.iloc[-1] ** (252 / n) - 1,
                vol=r.std() * np.sqrt(252),
                Sharpe=r.mean() / r.std() * np.sqrt(252),
                MDD=mdd)

def window(x):
    """Restrict to the common evaluation period."""
    return x.loc[eval_start:eval_end]

def perf_table(res):
    rows = []
    for k, df in res.items():
        d = window(df)
        p = perf(d["net"])
        p["Sharpe_gross"] = perf(d["gross"])["Sharpe"]                 # before costs
        p["turnover_per_year"] = d["turnover"].sum() / len(d) * 252
        p["time_in_ES"] = (d["exp_es"] != 0).mean()                    # share of days holding ES
        rows.append(pd.Series(p, name=k))
    t = pd.DataFrame(rows)
    return t[["CAGR", "vol", "Sharpe", "Sharpe_gross", "MDD", "turnover_per_year", "time_in_ES", "days"]]

def fmt(t, pct=("CAGR", "vol", "MDD", "time_in_ES")):
    """Format a table for printing (percent columns, 3-decimal numbers)."""
    t = t.copy()
    for c in t.columns:
        if c in pct:
            t[c] = (t[c] * 100).map(lambda v: f"{v:6.2f}%" if pd.notna(v) else "")
        elif c in ("days", "n_days", "stops"):
            t[c] = t[c].map(lambda v: f"{int(v)}" if pd.notna(v) else "")
        elif t[c].dtype.kind == "f":
            t[c] = t[c].map(lambda v: f"{v:6.3f}" if pd.notna(v) else "")
    return t


# ------------------------------------------------------------
# 5) Run and report
# ------------------------------------------------------------
res, s, pos, stops = build_all(extra_lag=0)     # main results
res_lag, _, _, _ = build_all(extra_lag=1)        # robustness: every signal one day later
MAIN = ["ES Trend (baseline)", "ES Trend + Regime filter", "ES Trend + Gold switch"]

# Table 1: overall performance
print("\n" + "=" * 70)
print(f"2) PERFORMANCE, net of {COST_BPS:.0f} bps costs  ({eval_start.date()} to {eval_end.date()})")
t1 = perf_table(res)
print(fmt(t1).to_string())

# Table 2: robustness to an extra day of delay
print("\n" + "=" * 70)
print("3) ROBUSTNESS: every signal delayed one more day")
t2 = pd.concat({"base": t1[["CAGR", "Sharpe", "MDD"]],
                "extra 1-day lag": perf_table(res_lag)[["CAGR", "Sharpe", "MDD"]]}, axis=1)
t2_print = t2.copy()
for c in t2_print.columns:
    t2_print[c] = t2_print[c].map(lambda v: f"{v*100:6.2f}%" if c[1] in ("CAGR", "MDD") else f"{v:6.3f}")
print(t2_print.to_string())

# Table 3: how the trend strategy behaves in each regime
print("\n" + "=" * 70)
print("4) REGIMES (state known at the previous close)")
reg = window(s["regime"])
base_net = window(res["ES Trend (baseline)"]["net"])
rows = []
for g in ["A", "B", "C", "D"]:
    m = reg == g
    p = perf(base_net[m])
    rows.append(pd.Series({"n_days": m.sum(), "share": m.mean(),
                           "ES Trend ann. return": base_net[m].mean() * 252,
                           "ES Trend Sharpe": p["Sharpe"]}, name=g))
t3 = pd.DataFrame(rows)
t3_print = t3.copy()
t3_print["n_days"] = t3_print["n_days"].astype(int)
t3_print["share"] = (t3_print["share"] * 100).map("{:.1f}%".format)
t3_print["ES Trend ann. return"] = (t3_print["ES Trend ann. return"] * 100).map("{:.2f}%".format)
t3_print["ES Trend Sharpe"] = t3_print["ES Trend Sharpe"].map("{:.3f}".format)
print("A: low VIX, Cu/Au normal | B: low VIX, Cu/Au risk-off | C: high VIX, Cu/Au normal | D: high VIX, Cu/Au risk-off")
print(t3_print.to_string())

# Table 4: inside regime D, days with vs. without recent BTC stress
print("\n" + "=" * 70)
print("5) D vs D+ (D days split by BTC risk-off in the last 10 days)")
Dm = window(s["D"]) == 1
Dp = window(s["D_plus"]) == 1
rows = []
for name in ["ES Trend (baseline)", "Gold Buy & Hold", "ES Trend + Gold switch"]:
    r = window(res[name]["net"])
    for lab, m in [("D (no BTC stress)", Dm & ~Dp), ("D+ (BTC stress)", Dp)]:
        p = perf(r[m])
        rows.append({"strategy": name, "group": lab, "n_days": int(m.sum()),
                     "ann_return": r[m].mean() * 252, "vol": p["vol"],
                     "Sharpe": p["Sharpe"], "MDD": p["MDD"]})
t4 = pd.DataFrame(rows)
t4_print = t4.copy()
for c in ["ann_return", "vol", "MDD"]:
    t4_print[c] = (t4_print[c] * 100).map(lambda v: f"{v:.2f}%" if pd.notna(v) else "")
t4_print["Sharpe"] = t4_print["Sharpe"].map(lambda v: f"{v:.3f}" if pd.notna(v) else "")
print(t4_print.to_string(index=False))
print("Note: D+ has very few days; treat this as descriptive, not statistically reliable.")

# Table 5: calendar-year returns
print("\n" + "=" * 70)
print("6) CALENDAR-YEAR RETURNS (net)")
yr = pd.DataFrame({k: window(res[k]["net"]).groupby(lambda d: d.year).apply(lambda x: (1 + x).prod() - 1)
                   for k in ["ES Buy & Hold", "Gold Buy & Hold", "50/50 ES + Gold Buy & Hold",
                             "ES Trend + Gold 50/50 (no switch)"] + MAIN})
yr.index.name = "year"
print((yr * 100).round(2).astype(str).add("%").to_string())

# Diversification: low correlation is why trend + gold works together
print("\nCorrelation of daily net returns, ES Trend vs Gold:",
      round(window(res["ES Trend (baseline)"]["net"]).corr(window(res["Gold Buy & Hold"]["net"])), 3))

# Stop-loss activity
print("\n" + "=" * 70)
print("7) STOP-LOSS EVENTS (ES Trend) per year")
st = pd.Series(1, index=pd.DatetimeIndex(stops))
st = window(st).groupby(lambda d: d.year).sum() if len(st) else pd.Series(dtype=int)
print(st.to_string() if len(st) else "none")


# ------------------------------------------------------------
# 6) Save tables (CSV with BOM so Excel opens them cleanly)
# ------------------------------------------------------------
t1.to_csv(f"{OUT_DIR}/table_performance.csv", encoding="utf-8-sig")
t2.to_csv(f"{OUT_DIR}/table_robustness_extra_lag.csv", encoding="utf-8-sig")
t3.to_csv(f"{OUT_DIR}/table_regimes.csv", encoding="utf-8-sig")
t4.to_csv(f"{OUT_DIR}/table_D_vs_Dplus.csv", index=False, encoding="utf-8-sig")
yr.to_csv(f"{OUT_DIR}/table_yearly_returns.csv", encoding="utf-8-sig")
daily = pd.DataFrame({k: res[k]["net"] for k in res})
window(daily).to_csv(f"{OUT_DIR}/daily_net_returns.csv", encoding="utf-8-sig")


# ------------------------------------------------------------
# 7) Figures (English labels, saved as PNG)
# ------------------------------------------------------------
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e6e5e1"   # text and grid colors
C1, C2, C3 = "#2a78d6", "#eb6834", "#1baf7a"        # series colors: blue, orange, aqua
GRAY, GRAY2 = "#8a8985", "#b9b8b3"                  # benchmarks

plt.rcParams.update({
    "font.size": 11, "axes.edgecolor": GRAY2, "axes.labelcolor": INK2,
    "xtick.color": INK2, "ytick.color": INK2, "axes.titlesize": 13,
    "axes.titleweight": "bold", "axes.titlecolor": INK, "axes.grid": True,
    "grid.color": GRID, "grid.linewidth": 0.8, "axes.spines.top": False,
    "axes.spines.right": False, "legend.frameon": False,
    "figure.facecolor": "white", "axes.facecolor": "white",
})

def save(fig, name):
    fig.tight_layout()
    fig.savefig(f"{OUT_DIR}/{name}.png", dpi=200, bbox_inches="tight")
    plt.show()

def shade(ax, flag, color, alpha):
    """Shade the periods where a 0/1 flag equals 1."""
    f = flag.fillna(0).astype(int)
    starts = f.index[(f.diff() == 1) | ((f == 1) & (f.index == f.index[0]))]
    ends = f.index[(f.diff() == -1)]
    for a in starts:
        b = ends[ends > a]
        ax.axvspan(a, b[0] if len(b) else f.index[-1], color=color, alpha=alpha, lw=0)

# Fig 1: cumulative wealth of the main strategies vs. buy & hold
fig, ax = plt.subplots(figsize=(10, 5.5))
series = [("ES Buy & Hold", GRAY, "--", 1.4), ("Gold Buy & Hold", GRAY2, "-", 1.4),
          ("ES Trend (baseline)", C1, "-", 2.0), ("ES Trend + Regime filter", C2, "-", 2.0),
          ("ES Trend + Gold switch", C3, "-", 2.4)]
for name, col, ls, lw in series:
    w = (1 + window(res[name]["net"])).cumprod()
    ax.plot(w.index, w.values, color=col, ls=ls, lw=lw, label=name)
    ax.annotate(f"{w.iloc[-1]:.2f}", (w.index[-1], w.iloc[-1]), xytext=(4, 0),
                textcoords="offset points", va="center", fontsize=9, color=INK2)
ax.axhline(1, color=GRAY2, lw=0.8)
ax.set_title(f"Cumulative wealth, net of {COST_BPS:.0f} bps costs")
ax.set_ylabel("Wealth (start = 1)")
ax.legend(loc="upper left", ncol=2, fontsize=9)
save(fig, "fig1_cumulative_wealth")

# Fig 2: VIX and its rolling median; shaded = high-VIX days
fig, ax = plt.subplots(figsize=(10, 4))
v = window(state)
shade(ax, v["vix_high"], C2, 0.12)
ax.plot(v.index, v["vix"], color=C1, lw=1.4, label="VIX close")
ax.plot(v.index, v["vix_med"], color=INK2, lw=1.2, ls="--", label="Rolling 1-year median")
ax.set_title("VIX regime: shaded = high VIX (VIX >= rolling median)")
ax.set_ylabel("VIX")
ax.legend(loc="upper left", bbox_to_anchor=(0, -0.1), ncol=2, fontsize=9)
save(fig, "fig2_vix_regime")

# Fig 3: Copper/Gold z-score; shaded = risk-off days
fig, ax = plt.subplots(figsize=(10, 4))
shade(ax, v["cuau_off"], C2, 0.12)
ax.plot(v.index, v["cuau_z"], color=C1, lw=1.4, label="Cu/Au z-score (3-year window)")
ax.axhline(CUAU_TH, color=INK2, ls="--", lw=1.2, label=f"Risk-off threshold ({CUAU_TH})")
ax.axhline(0, color=GRAY2, lw=0.8)
ax.set_title("Copper/Gold risk appetite: shaded = risk-off")
ax.set_ylabel("z-score")
ax.legend(loc="upper left", bbox_to_anchor=(0, -0.1), ncol=2, fontsize=9)
save(fig, "fig3_cuau_regime")

# Fig 4: days in each regime and the trend strategy's Sharpe ratio per regime
fig, axes = plt.subplots(1, 2, figsize=(10, 4))
names = ["A\nlow VIX\nCu/Au normal", "B\nlow VIX\nCu/Au risk-off",
         "C\nhigh VIX\nCu/Au normal", "D\nhigh VIX\nCu/Au risk-off"]
b = axes[0].bar(names, t3["n_days"], color=C1, width=0.6)
axes[0].bar_label(b, fmt="%d", padding=3, color=INK2, fontsize=9)
axes[0].set_title("Days in each regime")
axes[0].set_ylabel("Trading days")
b = axes[1].bar(names, t3["ES Trend Sharpe"], color=C1, width=0.6)
axes[1].bar_label(b, fmt="%.2f", padding=3, color=INK2, fontsize=9)
axes[1].axhline(0, color=GRAY, lw=0.8)
axes[1].set_title("ES Trend Sharpe ratio by regime")
for a in axes:
    a.tick_params(axis="x", labelsize=8.5)
    a.grid(axis="x", visible=False)
save(fig, "fig4_regimes")

# Fig 5: actual weights of the ES + Gold switch portfolio
fig, ax = plt.subplots(figsize=(10, 3.8))
e = window(res["ES Trend + Gold switch"])
ax.step(e.index, e["exp_gc"], where="post", color=C3, lw=1.4, label="Gold weight")
ax.step(e.index, e["exp_es"].abs(), where="post", color=C1, lw=1.2, label="ES weight (absolute)")
ax.set_ylim(-0.05, 1.1)
ax.set_title("ES + Gold switch: actual weights (100% gold in regime D)")
ax.set_ylabel("Weight")
ax.legend(loc="upper left", bbox_to_anchor=(0, -0.1), ncol=2, fontsize=9)
save(fig, "fig5_weights")

# Fig 6: Sharpe ratio in regime D, split by recent BTC stress
fig, ax = plt.subplots(figsize=(9, 4.8))
strats = t4["strategy"].unique()
x = np.arange(len(strats))
for i, (lab, col) in enumerate([("D (no BTC stress)", C1), ("D+ (BTC stress)", C2)]):
    vals = [t4[(t4.strategy == k) & (t4.group == lab)]["Sharpe"].iloc[0] for k in strats]
    ns = [t4[(t4.strategy == k) & (t4.group == lab)]["n_days"].iloc[0] for k in strats]
    b = ax.bar(x + (i - 0.5) * 0.36, np.nan_to_num(vals), width=0.34, color=col, label=lab)
    ax.bar_label(b, labels=[f"{v:.2f}\n(n={n})" if pd.notna(v) else f"n={n}" for v, n in zip(vals, ns)],
                 padding=3, fontsize=8.5, color=INK2)
ax.set_xticks(x, strats, fontsize=9)
ax.tick_params(axis="x", length=0, pad=12)
lo, hi = ax.get_ylim()
span = hi - lo
ax.set_ylim(min(lo, 0) - 0.25 * span, max(hi, 0) + 0.25 * span)   # room for the value labels
ax.axhline(0, color=GRAY, lw=0.8)
ax.set_title("Regime D vs D+ (BTC crash in last 10 days): Sharpe ratio", pad=12)
ax.legend(loc="upper left", bbox_to_anchor=(0, -0.12), ncol=2, fontsize=9)
ax.grid(axis="x", visible=False)
save(fig, "fig6_D_vs_Dplus")

print(f"\nAll tables and figures saved to: {OUT_DIR}")
