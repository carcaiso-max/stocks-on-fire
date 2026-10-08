"""Estrategia: cada trimestre (día 46 tras el cierre) comprar las N acciones con mayor aceleración de gestoras 13F
y mantenerlas 6 meses. Dos mitades de cartera que se renuevan en trimestres alternos. Curva diaria con costes.

Requiere haber ejecutado antes backtest_13f.py (usa su panel y su caché de precios).
Uso: python estrategia_aceleracion.py
"""
import numpy as np
import pandas as pd

import backtest_13f as bt

COSTS = [0.0, 0.001, 0.0025, 0.005]
TOPS = [10, 20, 50]
MIN_DOLLAR_VOL = 5e6
MIN_PRICE = 5


def load():
    df = pd.read_csv(bt.OUT_DIR / "panel.csv", parse_dates=["period", "entry"])
    df = df[df["ticker"].notna()]
    spy = bt.yahoo_history("SPY")["close"]
    closes = {}
    for tk in df["ticker"].unique():
        px = bt.yahoo_history(tk)
        if px is not None:
            closes[tk] = px["close"]
    return df, spy, closes


def tranche_path(tickers, entry, exit_, closes, days):
    """Valor diario de 1 $ repartido a partes iguales; si una acción deja de cotizar se queda en su último precio."""
    window = days[(days >= entry) & (days <= exit_)]
    paths = []
    for tk in tickers:
        c = closes[tk]
        p0 = c.asof(entry)
        if not p0 or p0 != p0:
            continue
        s = c[(c.index >= entry) & (c.index <= exit_)].reindex(window).ffill().fillna(p0)
        paths.append(s / p0)
    return pd.concat(paths, axis=1).mean(axis=1) if paths else pd.Series(1.0, index=window)


def run(df, spy, closes, picker, cost):
    days = spy.index
    entries = df.groupby("period")["entry"].first().sort_index()
    periods = list(entries.index)
    sleeves = [[], []]
    for i, p in enumerate(periods):
        entry = entries[p]
        nxt = entries[periods[i + 2]] if i + 2 < len(periods) else days[min(days.searchsorted(entry) + 126, len(days) - 1)]
        tickers = picker(df[df["period"] == p])
        path = tranche_path(tickers, entry, nxt, closes, days)
        path = path * (1 - cost)
        path.iloc[-1] *= (1 - cost)
        sleeves[i % 2].append(path)
    curves = []
    for k, parts in enumerate(sleeves):
        eq, level = [], 0.5
        for part in parts:
            seg = part * level
            eq.append(seg.iloc[:-1] if part is not parts[-1] else seg)
            level = seg.iloc[-1]
        curves.append(pd.concat(eq))
    start = entries.iloc[0]
    idx = days[(days >= start) & (days <= max(c.index.max() for c in curves))]
    a = curves[0].reindex(idx).ffill()
    b = curves[1].reindex(idx).ffill().fillna(0.5)
    return (a + b).dropna()


def stats(eq):
    eq = eq / eq.iloc[0]
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    r = eq.pct_change().dropna()
    dd = (eq / eq.cummax() - 1).min()
    return {"CAGR %": (eq.iloc[-1] ** (1 / yrs) - 1) * 100, "vol %": r.std() * np.sqrt(252) * 100,
            "Sharpe": r.mean() / r.std() * np.sqrt(252), "MDD %": dd * 100, "x final": eq.iloc[-1]}


def yearly(eq):
    y = eq.resample("YE").last()
    return (y / y.shift(1) - 1).dropna() * 100


def main():
    df, spy, closes = load()
    df = df[df["ticker"].isin(closes)]
    df = df[(df["dollar_vol"] >= MIN_DOLLAR_VOL)]
    df = df[df["entry"].notna()]
    px_entry = [closes[t].asof(e) for t, e in zip(df["ticker"], df["entry"])]
    df = df[np.array(px_entry, dtype=float) >= MIN_PRICE]
    print(f"universo: {len(df):,} obs, {df['period'].nunique()} trimestres, media {len(df) / df['period'].nunique():.0f} por trimestre "
          f"(≥{MIN_DOLLAR_VOL / 1e6:.0f} M$/día, precio ≥{MIN_PRICE} $)")

    spy_eq = spy[spy.index >= df["entry"].min()]
    res = {"SPY": spy_eq}
    univ = run(df, spy, closes, lambda g: g["ticker"].tolist(), 0.0)
    res["Universo (todas, iguales)"] = univ
    rows = []
    for n in TOPS:
        for c in COSTS:
            eq = run(df, spy, closes, lambda g, n=n: g.nlargest(n, "accel")["ticker"].tolist(), c)
            res[f"Top {n} aceleración, coste {c * 100:.2f}%"] = eq
        res[f"Peor {n} aceleración, coste 0%"] = run(df, spy, closes, lambda g, n=n: g.nsmallest(n, "accel")["ticker"].tolist(), 0.0)

    for name, eq in res.items():
        rows.append({"cartera": name, **stats(eq)})
    tab = pd.DataFrame(rows).set_index("cartera")
    pd.set_option("display.width", 200)
    print(tab.round(2).to_string())

    print("\n=== CAGR por tramos (%) ===")
    tr = {}
    for name in ["SPY", "Universo (todas, iguales)", "Top 20 aceleración, coste 0.25%", "Top 10 aceleración, coste 0.25%",
                 "Top 50 aceleración, coste 0.25%", "Peor 20 aceleración, coste 0%"]:
        eq = res[name]
        tr[name] = {f"{lo}-{hi}": stats(eq[(eq.index.year >= lo) & (eq.index.year <= hi)])["CAGR %"]
                    for lo, hi in ((2014, 2017), (2018, 2021), (2022, 2026))}
    print(pd.DataFrame(tr).T.round(1).to_string())

    print("\n=== Año a año: Top 20 (coste 0,25 %) vs SPY y universo (%) ===")
    yy = pd.DataFrame({"Top20": yearly(res["Top 20 aceleración, coste 0.25%"]), "SPY": yearly(res["SPY"]),
                       "Universo": yearly(res["Universo (todas, iguales)"])})
    yy["vs SPY"] = yy["Top20"] - yy["SPY"]
    yy["vs univ"] = yy["Top20"] - yy["Universo"]
    print(yy.round(1).to_string())
    print(f"años que gana a SPY: {(yy['vs SPY'] > 0).sum()}/{len(yy)}; al universo: {(yy['vs univ'] > 0).sum()}/{len(yy)}")
    tab.to_csv(bt.OUT_DIR / "estrategia_aceleracion.csv")
    pd.DataFrame(res).to_csv(bt.OUT_DIR / "estrategia_aceleracion_curvas.csv")


if __name__ == "__main__":
    main()
