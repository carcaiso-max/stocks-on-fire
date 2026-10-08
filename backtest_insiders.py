"""Backtest de compras de directivos (formulario 4), 2006 → hoy, sin look-ahead:
entrada al cierre del primer día hábil posterior a la fecha de PUBLICACIÓN del formulario.

Uso: python backtest_insiders.py     (requiere haber ejecutado insiders.py)
"""
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from build_13f import DB_PATH, log
from insiders import INS_DIR

PX_DIR = INS_DIR / "precios"
OUT_DIR = INS_DIR
CHART_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/{}?period1=1136073600&period2="
             + str(int(time.time())) + "&interval=1d")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
US_TICKER_RE = re.compile(r"^[A-Z]{1,5}([.-][A-Z]{1,2})?$")
ENTITY_RE = re.compile(r"\b(INC|CORP|CORPORATION|LLC|L\.L\.C|LP|L\.P|LTD|FUND|FUNDS|CAPITAL|PARTNERS|HOLDINGS?|TRUST|"
                       r"MANAGEMENT|ADVISORS?|GROUP|INVESTMENTS?|VENTURES?|FOUNDATION|BANK|PLC|AG|SA|GMBH|CO)\b", re.I)
PEOPLE_ROLES = {"CEO", "CFO", "Directivo", "Consejero"}
HORIZONS = {"1m": 21, "3m": 63, "6m": 126}
SPLITS = [1.5, 2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40, 50]
DEDUP_DAYS = 90


def log_safe(msg):
    log(msg)


def yahoo(ticker):
    sym = ticker.replace(".", "-")
    path = PX_DIR / f"{sym}.json"
    if path.exists():
        raw = json.loads(path.read_text())
    else:
        raw = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(CHART_URL.format(sym), headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as r:
                    res = json.loads(r.read())["chart"]["result"][0]
                q = res["indicators"]["quote"][0]
                raw = {"t": res.get("timestamp", []), "c": q.get("close", []), "v": q.get("volume", [])}
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(20 * (attempt + 1))
                    continue
                raw = {"t": []}
                break
            except (OSError, KeyError, TypeError, ValueError):
                raw = {"t": []}
                break
        if raw is None:
            return None
        path.write_text(json.dumps(raw))
    if not raw.get("t"):
        return None
    df = pd.DataFrame({"close": raw["c"], "volume": raw["v"]},
                      index=pd.to_datetime(raw["t"], unit="s").normalize()).dropna(subset=["close"])
    df = df[~df.index.duplicated(keep="last")]
    return df if len(df) > 60 else None


def load_buys():
    con = sqlite3.connect(DB_PATH)
    b = pd.read_sql("SELECT * FROM insider_trades WHERE code = 'P'", con, parse_dates=["filing_date", "trans_date"])
    con.close()
    b = b[b["role"].isin(PEOPLE_ROLES) & ~b["owner"].fillna("").str.contains(ENTITY_RE)]
    b = b[~b["plan_10b51"].astype(bool)]
    b = b[b["ticker"].fillna("").str.match(US_TICKER_RE)]
    b = b[(b["filing_date"] - b["trans_date"]).dt.days.between(0, 30)]
    b = b[b["value"] >= 5_000]
    prev = b["owned_after"] - b["shares"]
    b["pct_increase"] = np.where(prev > 0, b["shares"] / prev, np.inf)
    return b


def events(b):
    """Una fila por (ticker, fecha de publicación) con lo que se sabía ese día."""
    b = b.sort_values("filing_date")
    out = []
    for tk, g in b.groupby("ticker"):
        fds = g["filing_date"].drop_duplicates()
        for d in fds:
            win = g[(g["filing_date"] <= d) & (g["trans_date"] >= d - pd.Timedelta(days=30))]
            today = g[g["filing_date"] == d]
            out.append({
                "ticker": tk, "filing_date": d,
                "n_insiders_30d": win["owner_cik"].nunique(),
                "value_today": today["value"].sum(),
                "max_value_today": today.groupby("owner_cik")["value"].sum().max(),
                "ceo_cfo_value_today": today.loc[today["role"].isin(["CEO", "CFO"])].groupby("owner_cik")["value"].sum().max()
                if today["role"].isin(["CEO", "CFO"]).any() else 0.0,
                "max_pct_increase": today["pct_increase"].replace(np.inf, 10).max(),
                "ref_price": today["price"].median(), "ref_date": today["trans_date"].max(),
            })
    return pd.DataFrame(out)


SIGNALS = {
    "A cualquier compra ≥25k$": lambda e: e["max_value_today"] >= 25_000,
    "B CEO/CFO ≥100k$": lambda e: e["ceo_cfo_value_today"] >= 100_000,
    "C grupo ≥2 en 30 días": lambda e: e["n_insiders_30d"] >= 2,
    "D grupo ≥3 en 30 días": lambda e: e["n_insiders_30d"] >= 3,
    "E sube participación ≥20 % (≥25k$)": lambda e: (e["max_pct_increase"] >= 0.2) & (e["max_value_today"] >= 25_000),
}


def dedup(e):
    """Como mucho una señal por ticker cada 90 días (la primera)."""
    keep, last = [], {}
    for r in e.sort_values("filing_date").itertuples():
        lt = last.get(r.ticker)
        if lt is None or (r.filing_date - lt).days >= DEDUP_DAYS:
            keep.append(r.Index)
            last[r.ticker] = r.filing_date
    return e.loc[keep]


def valid_ticker(px, ref_date, ref_price):
    c = px["close"].asof(ref_date)
    if not c or c != c:
        return False
    r = c / ref_price
    cands = np.array([1.0] + [x for k in SPLITS for x in (k, 1 / k)])
    return np.abs(r / cands - 1).min() < 0.15


def forward(e, prices, spy, iwm):
    rows = []
    for r in e.itertuples():
        px = prices.get(r.ticker)
        if px is None or not valid_ticker(px, r.ref_date, r.ref_price):
            continue
        c = px["close"]
        k = c.index.searchsorted(r.filing_date, side="right")
        if k >= len(c) or (c.index[k] - r.filing_date).days > 7:
            continue
        entry_d, p0 = c.index[k], c.iloc[k]
        dv = (c.iloc[max(0, k - 50):k] * px["volume"].iloc[max(0, k - 50):k]).mean()
        row = {"ticker": r.ticker, "filing_date": r.filing_date, "entry": entry_d, "price": p0, "dollar_vol": dv}
        for hn, hd in HORIZONS.items():
            target = spy.index[min(spy.index.searchsorted(entry_d) + hd, len(spy) - 1)]
            if target <= entry_d or spy.index.searchsorted(entry_d) + hd >= len(spy):
                continue
            p1 = c.asof(target)
            ret = p1 / p0 - 1
            row[f"ret_{hn}"] = ret * 100
            row[f"xs_spy_{hn}"] = (ret - (spy.asof(target) / spy.asof(entry_d) - 1)) * 100
            row[f"xs_iwm_{hn}"] = (ret - (iwm.asof(target) / iwm.asof(entry_d) - 1)) * 100
        rows.append(row)
    return pd.DataFrame(rows)


def summarize(f, label):
    lines = []
    for hn in HORIZONS:
        col = f"xs_iwm_{hn}"
        d = f.dropna(subset=[col])
        if len(d) < 30:
            continue
        m = d.groupby(d["entry"].dt.to_period("M"))[col].mean()
        t = m.mean() / (m.std(ddof=1) / np.sqrt(len(m)))
        lines.append(f"  {hn}: n={len(d):5d}  exceso vs IWM media {d[col].mean():6.2f}  mediana {d[col].median():6.2f}  "
                     f"gana {(d[col] > 0).mean():4.0%}  vs SPY media {d[f'xs_spy_{hn}'].mean():6.2f}  t(meses) {t:5.2f}")
    return f"{label}\n" + "\n".join(lines)


def cost_side(dv):
    return np.where(dv >= 50e6, 0.001, np.where(dv >= 10e6, 0.0025, 0.005))


def portfolio(f, prices, spy, hold=63, max_pos=40):
    """Cartera en tiempo real: cada señal entra con 1/max_pos del capital (si hay hueco) y sale a los `hold` días."""
    days = spy.index[spy.index >= f["entry"].min()]
    f = f.sort_values("entry")
    cash, positions, equity = 1.0, [], []
    sig_by_day = {d: g for d, g in f.groupby("entry")}
    for i, d in enumerate(days):
        still = []
        for p in positions:
            px = prices[p["ticker"]]["close"].asof(d)
            p["value"] = p["units"] * (px if px == px else p["last"])
            p["last"] = px if px == px else p["last"]
            if i >= p["exit_i"]:
                cash += p["value"] * (1 - p["cost"])
            else:
                still.append(p)
        positions = still
        nav = cash + sum(p["value"] for p in positions)
        if d in sig_by_day:
            for r in sig_by_day[d].itertuples():
                if len(positions) >= max_pos:
                    break
                size = min(nav / max_pos, cash)
                if size <= 0:
                    break
                c = float(cost_side(np.array([r.dollar_vol]))[0])
                units = size * (1 - c) / r.price
                positions.append({"ticker": r.ticker, "units": units, "value": size * (1 - c), "last": r.price,
                                  "exit_i": i + hold, "cost": c})
                cash -= size
        equity.append(cash + sum(p["value"] for p in positions))
    return pd.Series(equity, index=days)


def stats(eq):
    eq = eq / eq.iloc[0]
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    r = eq.pct_change().dropna()
    return {"CAGR %": (eq.iloc[-1] ** (1 / yrs) - 1) * 100, "vol %": r.std() * np.sqrt(252) * 100,
            "Sharpe": r.mean() / r.std() * np.sqrt(252), "MDD %": (eq / eq.cummax() - 1).min() * 100}


def main():
    PX_DIR.mkdir(parents=True, exist_ok=True)
    b = load_buys()
    log(f"{len(b):,} compras de personas (CEO/CFO/directivos/consejeros) en {b['ticker'].nunique():,} tickers")
    e = events(b)
    log(f"{len(e):,} días-evento")
    tickers = sorted(e["ticker"].unique()) + ["SPY", "IWM"]
    todo = [t for t in tickers if not (PX_DIR / f"{t.replace('.', '-')}.json").exists()]
    log(f"Yahoo: {len(tickers)} tickers, {len(todo)} por descargar")
    with ThreadPoolExecutor(6) as ex:
        for i, _ in enumerate(ex.map(yahoo, todo), 1):
            if i % 500 == 0:
                log(f"  {i}/{len(todo)}")
    prices = {t: p for t in tickers if (p := yahoo(t)) is not None}
    spy, iwm = prices["SPY"]["close"], prices["IWM"]["close"]

    lines = [f"Compras: {len(b):,} · eventos: {len(e):,} · tickers con precio: {len(prices) - 2:,} de {len(tickers) - 2:,}",
             "Exceso de rentabilidad (%) tras la publicación; t calculada sobre medias mensuales.",
             "Filtros de operabilidad: precio ≥5 $ y volumen medio ≥1 M$/día salvo indicación."]
    results = {}
    for name, rule in SIGNALS.items():
        ev = dedup(e[rule(e)])
        f = forward(ev, prices, spy, iwm)
        if f.empty:
            continue
        f["signal"] = name
        results[name] = f
        trad = f[(f["price"] >= 5) & (f["dollar_vol"] >= 1e6)]
        lines.append("\n" + summarize(trad, f"=== {name} (operables) ==="))
        liq = f[(f["price"] >= 5) & (f["dollar_vol"] >= 20e6)]
        lines.append(summarize(liq, "  -- solo líquidas (≥20 M$/día):"))
        lines.append("  -- por tramos (operables, 3m vs IWM):")
        for lo, hi in ((2006, 2012), (2013, 2019), (2020, 2026)):
            s = trad[(trad["entry"].dt.year >= lo) & (trad["entry"].dt.year <= hi)].dropna(subset=["xs_iwm_3m"])
            if len(s) > 20:
                lines.append(f"     {lo}-{hi}: n={len(s):5d} media {s['xs_iwm_3m'].mean():6.2f} mediana {s['xs_iwm_3m'].median():6.2f} "
                             f"gana {(s['xs_iwm_3m'] > 0).mean():4.0%}")
    pd.concat(results.values()).to_csv(OUT_DIR / "eventos.csv", index=False)

    lines.append("\n=== Cartera simulada (operables, 3 meses por posición, máx. 40 posiciones, costes según liquidez) ===")
    base = {"SPY": prices["SPY"]["close"], "IWM": prices["IWM"]["close"]}
    tab = []
    for name in ("C grupo ≥2 en 30 días", "D grupo ≥3 en 30 días", "B CEO/CFO ≥100k$", "A cualquier compra ≥25k$"):
        if name not in results:
            continue
        f = results[name]
        f = f[(f["price"] >= 5) & (f["dollar_vol"] >= 1e6)]
        eq = portfolio(f, prices, spy)
        tab.append({"cartera": name, **stats(eq)})
        for lo, hi in ((2006, 2012), (2013, 2019), (2020, 2026)):
            seg = eq[(eq.index.year >= lo) & (eq.index.year <= hi)]
            tab[-1][f"CAGR {lo}-{hi}"] = stats(seg)["CAGR %"]
        start = eq.index[0]
    for k, s in base.items():
        s = s[s.index >= start]
        tab.append({"cartera": k, **stats(s), **{f"CAGR {lo}-{hi}": stats(s[(s.index.year >= lo) & (s.index.year <= hi)])["CAGR %"]
                                                 for lo, hi in ((2006, 2012), (2013, 2019), (2020, 2026))}})
    lines.append(pd.DataFrame(tab).set_index("cartera").round(2).to_string())
    text = "\n".join(lines)
    (OUT_DIR / "resultados_insiders.txt").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
