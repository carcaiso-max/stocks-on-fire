"""Backtest de valoración y crecimiento con fundamentales de la SEC, sin look-ahead.

En cada trimestre del panel de backtest_13f.py (entrada el día 46 tras el cierre) se usan solo las cifras publicadas
antes de la entrada. Capitalización = precio implícito en los 13F a cierre de trimestre (valor ÷ acciones, sin ajustar
por splits) × acciones en circulación declaradas a la SEC (también sin ajustar).

Requiere: backtest_13f.py y fundamentales.py ya ejecutados.
Uso: python backtest_fundamentales.py
"""
import sqlite3

import numpy as np
import pandas as pd

import backtest_13f as bt
import estrategia_aceleracion as ea
from build_13f import DB_PATH, log

FEATURES = {
    "ep": "Rentab. beneficio (E/P)",
    "fcf_yield": "Rentab. flujo caja libre",
    "bm": "Valor contable / capitalización",
    "revenue_growth": "Crecimiento ventas 1 año",
    "net_income_growth": "Crecimiento beneficio 1 año",
    "net_margin": "Margen neto",
    "neg_peg": "PEG histórico (invertido: más alto = más barato)",
}


def load_facts(ciks):
    con = sqlite3.connect(DB_PATH)
    marks = ",".join("?" * len(ciks))
    f = pd.read_sql(f"SELECT * FROM fund_facts WHERE cik IN ({marks})", con, params=[int(c) for c in ciks], parse_dates=["end", "filed"])
    con.close()
    return dict(tuple(f.groupby("cik")))


def point_in_time(g, asof):
    """Última cifra de cada métrica publicada antes de `asof` y su valor un año antes."""
    v = g[(g["filed"] <= asof) & (g["end"] <= asof)]
    out = {}
    for m, s in v.groupby("metric"):
        s = s.sort_values(["end", "filed"])
        last = s.iloc[-1]
        out[m] = last["value"]
        out[m + "_age"] = (asof - last["end"]).days
        if m in ("revenue", "net_income"):
            prev = s[(s["end"] - (last["end"] - pd.DateOffset(years=1))).abs().dt.days <= 20]
            p = prev["value"].iloc[-1] if len(prev) else None
            out[m + "_growth"] = (last["value"] / p - 1) * 100 if p and p > 0 else np.nan
    return out


def build_panel():
    panel = pd.read_csv(bt.OUT_DIR / "panel.csv", parse_dates=["period", "entry"])
    con = sqlite3.connect(DB_PATH)
    m = pd.read_sql("SELECT ticker, cik FROM cik_tickers", con)
    st = pd.read_sql("SELECT period, cusip, shares, value FROM stats WHERE n_filers >= 30", con, parse_dates=["period"])
    con.close()
    tk2cik = dict(zip(m["ticker"], m["cik"]))
    panel["cik"] = panel["ticker"].fillna("").str.upper().str.replace("/", ".").str.replace("-", ".").map(tk2cik)
    st["px13f"] = st["value"] / st["shares"].where(st["shares"] > 0)
    panel = panel.merge(st[["period", "cusip", "px13f"]], on=["period", "cusip"], how="left")
    log(f"panel: {len(panel):,} obs; con CIK {panel['cik'].notna().mean():.0%}")
    facts = load_facts(sorted(panel["cik"].dropna().astype(int).unique()))
    rows = []
    for cik, g in panel.dropna(subset=["cik"]).groupby("cik"):
        fg = facts.get(int(cik))
        if fg is None:
            continue
        for r in g.itertuples():
            pit = point_in_time(fg, r.entry - pd.Timedelta(days=1))
            pit["idx"] = r.Index
            rows.append(pit)
    f = pd.DataFrame(rows).set_index("idx")
    df = panel.join(f)
    fresh = lambda c: df[c].where(df[c + "_age"] <= 400) if c + "_age" in df else df[c]
    shares = fresh("shares")
    df["mcap"] = df["px13f"] * shares
    ni, rev, ocf = fresh("net_income"), fresh("revenue"), fresh("ocf")
    capex = df["capex"].where(df.get("capex_age", pd.Series(0, index=df.index)) <= 400).fillna(0)
    ok = df["mcap"] > 1e7
    df["ep"] = (ni / df["mcap"] * 100).where(ok)
    df["fcf_yield"] = ((ocf - capex) / df["mcap"] * 100).where(ok)
    df["bm"] = (fresh("equity") / df["mcap"]).where(ok)
    df["net_margin"] = (ni / rev * 100).where(rev > 0)
    pe = (df["mcap"] / ni).where(ok & (ni > 0))
    df["peg_hist"] = (pe / df["net_income_growth"]).where(df["net_income_growth"] > 0)
    df["neg_peg"] = -df["peg_hist"]
    df.to_csv(bt.OUT_DIR / "panel_fundamentales.csv", index=False)
    return df


def top_filtered(df, mask, n=20, ret="xs_6m", col="accel"):
    d = df.dropna(subset=[ret])
    allm = d.groupby("period")[ret].mean()
    top = d[mask.reindex(d.index).fillna(False)].sort_values(col, ascending=False).groupby("period").head(n)
    diff = top.groupby("period")[ret].mean() - allm
    diff = diff.dropna()
    return diff.mean(), diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff))), (diff > 0).mean(), len(diff)


def main():
    df = build_panel()
    lines = [f"Panel: {len(df):,} obs · con capitalización {df['mcap'].notna().mean():.0%} · "
             f"con E/P {df['ep'].notna().mean():.0%} · con PEG histórico {df['peg_hist'].notna().mean():.0%}",
             "Cobertura de E/P por año: " + ", ".join(f"{y}: {v:.0%}" for y, v in
                                                       df.groupby(df['period'].dt.year)['ep'].apply(lambda s: s.notna().mean()).items())]
    for ret in ("xs_3m", "xs_6m"):
        lines.append(f"\n=== Exceso vs SPY {ret[3:]} por quintiles (Q1 = valor más bajo, Q5 = más alto) ===")
        lines.append(f"{'métrica':34} {'Q1':>7} {'Q2':>7} {'Q3':>7} {'Q4':>7} {'Q5':>7} {'Q5-Q1':>7} {'t':>6} {'%T>0':>5}")
        for col, label in FEATURES.items():
            if df[col].notna().sum() < 2000:
                continue
            q, s, t, hit, n = bt.quintile_table(df, col, ret)
            lines.append(f"{label[:34]:34} " + " ".join(f"{q.get(i, np.nan):7.2f}" for i in range(1, 6)) + f" {s:7.2f} {t:6.2f} {hit:5.0%}")

    lines.append("\n=== Por tramos: Q5-Q1 a 6 meses (t) ===")
    for col in ("ep", "fcf_yield", "neg_peg", "revenue_growth"):
        parts = []
        for lo, hi in ((2014, 2017), (2018, 2021), (2022, 2026)):
            sub = df[(df["period"].dt.year >= lo) & (df["period"].dt.year <= hi)]
            q, s, t, hit, n = bt.quintile_table(sub, col, "xs_6m")
            parts.append(f"{lo}-{hi}: {s:6.2f} ({t:5.2f})")
        lines.append(f"{FEATURES[col][:34]:34} " + "  ".join(parts))

    lines.append("\n=== Top 20 por aceleración con filtros de valoración (exceso sobre el universo, 6 m) ===")
    med_ep = df.groupby("period")["ep"].transform("median")
    combos = {
        "sin filtro": pd.Series(True, index=df.index),
        "con beneficios (E/P > 0)": df["ep"] > 0,
        "mitad barata (E/P ≥ mediana)": (df["ep"] > 0) & (df["ep"] >= med_ep),
        "mitad cara (E/P < mediana o pérdidas)": ~((df["ep"] > 0) & (df["ep"] >= med_ep)) & df["ep"].notna(),
        "flujo de caja libre > 0": df["fcf_yield"] > 0,
        "PEG histórico ≤ 1,5": df["peg_hist"].between(0, 1.5),
        "ventas crecen > 10 %": df["revenue_growth"] > 10,
    }
    for name, mask in combos.items():
        d, t, hit, n = top_filtered(df, mask)
        lines.append(f"{name:40} {d:6.2f} pt  t {t:5.2f}  gana {hit:4.0%}  ({n} trimestres)")

    lines.append("\n=== Cartera (top 20 aceleración, 6 m, coste 0,25 %/lado, ≥5 M$/día, precio ≥5 $) ===")
    _, spy, closes = ea.load()
    base = df[df["ticker"].isin(closes) & (df["dollar_vol"] >= ea.MIN_DOLLAR_VOL)].copy()
    px = np.array([closes[t].asof(e) for t, e in zip(base["ticker"], base["entry"])], dtype=float)
    base = base[px >= ea.MIN_PRICE]
    base["cheap"] = (base["ep"] > 0) & (base["ep"] >= base.groupby("period")["ep"].transform("median"))
    tab = []
    pickers = {
        "Top 20 aceleración": lambda g: g.nlargest(20, "accel")["ticker"].tolist(),
        "Top 20 aceleración con E/P > 0": lambda g: g[g["ep"] > 0].nlargest(20, "accel")["ticker"].tolist(),
        "Top 20 aceleración mitad barata": lambda g: g[g["cheap"]].nlargest(20, "accel")["ticker"].tolist(),
        "Top 20 aceleración con FCF > 0": lambda g: g[g["fcf_yield"] > 0].nlargest(20, "accel")["ticker"].tolist(),
        "Top 20 aceleración PEG hist. ≤ 1,5": lambda g: g[g["peg_hist"].between(0, 1.5)].nlargest(20, "accel")["ticker"].tolist(),
        "Top 20 más baratas (E/P)": lambda g: g.nlargest(20, "ep")["ticker"].tolist(),
        "Universo (todas, iguales)": lambda g: g["ticker"].tolist(),
    }
    curves = {"SPY": spy[spy.index >= base["entry"].min()]}
    for name, pick in pickers.items():
        curves[name] = ea.run(base, spy, closes, pick, 0.0 if name.startswith("Universo") else 0.0025)
    for name, eq in curves.items():
        row = {"cartera": name, **ea.stats(eq)}
        for lo, hi in ((2014, 2017), (2018, 2021), (2022, 2026)):
            row[f"CAGR {lo}-{hi}"] = ea.stats(eq[(eq.index.year >= lo) & (eq.index.year <= hi)])["CAGR %"]
        tab.append(row)
    lines.append(pd.DataFrame(tab).set_index("cartera").round(2).to_string())
    text = "\n".join(lines)
    (bt.OUT_DIR / "resultados_fundamentales.txt").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
