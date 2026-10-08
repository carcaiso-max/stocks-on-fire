"""Backtest de la puntuación de Sugerencias: ¿las acciones mejor puntuadas rinden más después?

Sin look-ahead: en cada trimestre se usan solo las gestoras presentadas en plazo (n_on_time) y se entra
al cierre del primer día hábil tras el día 46 desde el fin del trimestre.

Uso:
    python backtest_13f.py            descarga lo que falte (tickers y precios) y calcula
"""
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from build_13f import DATA_DIR, DB_PATH

BT_DIR = DATA_DIR / "backtest"
FIGI_FILE = BT_DIR / "figi_map.json"
PX_DIR = BT_DIR / "precios"
OUT_DIR = BT_DIR
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?period1=1356998400&period2=" + str(int(time.time())) + "&interval=1d"
US_TICKER_RE = re.compile(r"^[A-Z]{1,5}([./ -][A-Z]{1,2})?$")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
FUND_TYPES = {"ETP", "Mutual Fund", "Closed-End Fund", "Unit Inv Tr", "Open-End Fund", "ETF", "ETN"}
FUND_NAME_RE = (r"\bETFS?\b|\bETN\b|ISHARES|SPDR|PROSHARES|VANECK|WISDOMTREE|DIREXION|SELECT SECTOR|"
                r"\bFDS?\b|\bFUNDS?\b|INDEX F|EXCHANGE TRADED|INVESCO QQQ|GLOBAL X|ETF TR")
MIN_BASE = 100
LAG_DAYS = 46
HORIZONS = {"3m": 63, "6m": 126}
SPLITS = [1.5, 2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40, 50]


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def load_panel():
    con = sqlite3.connect(DB_PATH)
    st = pd.read_sql("SELECT period, cusip, n_filers, n_on_time, shares, value FROM stats WHERE n_filers >= 30", con)
    sec = pd.read_sql("SELECT cusip, name FROM securities", con).set_index("cusip")["name"]
    con.close()
    st["period"] = pd.to_datetime(st["period"])
    tot = st.groupby("period")["n_filers"].sum()
    valid = tot[(tot >= 0.25 * tot.max()) & tot.index.is_quarter_end].index
    st = st[st["period"].isin(valid)]
    return st, sec, sorted(valid)


def features(st, periods):
    """Métricas 13F punto a punto con n_on_time."""
    n = st.pivot(index="cusip", columns="period", values="n_on_time").reindex(columns=periods).fillna(0)
    rows = []
    for i in range(4, len(periods)):
        p, p2, p4 = periods[i], periods[i - 2], periods[i - 4]
        base = n[p4]
        u = n.index[base >= MIN_BASE]
        if len(u) == 0:
            continue
        sub = n.loc[u, periods[i - 4:i + 1]].to_numpy()
        f = pd.DataFrame({"cusip": u, "period": p, "n_before": sub[:, 0], "n_now": sub[:, 4]})
        f["d_filers"] = f["n_now"] - f["n_before"]
        f["pct_filers"] = (f["n_now"] / f["n_before"] - 1) * 100
        f["d_recent"] = sub[:, 4] - sub[:, 2]
        f["d_prior"] = sub[:, 2] - sub[:, 0]
        f["accel"] = (f["d_recent"] - f["d_prior"]) / f["n_before"] * 100
        f["consistency"] = (np.diff(sub, axis=1) > 0).sum(axis=1)
        rows.append(f)
    return pd.concat(rows, ignore_index=True)


def http_json(url, data=None, headers=None):
    h = {"User-Agent": UA}
    h.update(headers or {})
    body = json.dumps(data).encode() if data is not None else None
    if body:
        h["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h), timeout=30) as r:
        return json.loads(r.read())


def map_tickers(cusips):
    BT_DIR.mkdir(parents=True, exist_ok=True)
    cache = json.loads(FIGI_FILE.read_text()) if FIGI_FILE.exists() else {}
    missing = [c for c in cusips if c not in cache]
    log(f"OpenFIGI: {len(cusips) - len(missing)} en caché, {len(missing)} por consultar (~{len(missing) / 250:.0f} min)")
    for i in range(0, len(missing), 10):
        batch = missing[i:i + 10]
        for attempt in range(6):
            try:
                res = http_json(OPENFIGI_URL, [{"idType": "ID_CUSIP", "idValue": c} for c in batch])
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(15)
                    continue
                raise
            except OSError:
                time.sleep(10)
        else:
            log("OpenFIGI no responde; se continúa con lo que hay")
            break
        for c, r in zip(batch, res):
            rows = r.get("data") or []
            row = next((x for x in rows if x.get("exchCode") == "US"), rows[0] if rows else {})
            cache[c] = {"ticker": row.get("ticker"), "type": row.get("securityType"), "type2": row.get("securityType2")}
        if (i // 10) % 25 == 24:
            FIGI_FILE.write_text(json.dumps(cache))
            log(f"  {i + 10}/{len(missing)}")
        time.sleep(2.45)
    FIGI_FILE.write_text(json.dumps(cache))
    return cache


def yahoo_symbol(ticker):
    return re.sub(r"[^A-Z0-9\-]", "", ticker.upper().replace(".", "-").replace("/", "-").replace(" ", "-"))


def px_path(ticker):
    return PX_DIR / f"{yahoo_symbol(ticker) or '_'}.json"


def yahoo_history(ticker):
    sym = yahoo_symbol(ticker)
    if not sym:
        return None
    path = px_path(ticker)
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
                raw = {"t": res.get("timestamp", []), "h": q.get("high", []), "c": q.get("close", []), "v": q.get("volume", [])}
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
    df = pd.DataFrame({"date": pd.to_datetime(raw["t"], unit="s").normalize(), "high": raw["h"], "close": raw["c"],
                       "volume": raw["v"]}).dropna(subset=["close"]).drop_duplicates("date", keep="last")
    return df.set_index("date") if len(df) > 260 else None


def validate(px, implied):
    """El ticker de Yahoo es la misma empresa si el cociente precio Yahoo / precio implícito 13F
    solo cambia por splits entre trimestres consecutivos."""
    yc = px["close"].reindex(implied.index, method="ffill")
    r = (yc / implied).dropna()
    r = r[(r > 0) & np.isfinite(r)]
    if len(r) < 3:
        return False
    q = (r / r.shift(1)).dropna().to_numpy()
    cands = np.array([1.0] + [c for k in SPLITS for c in (k, 1 / k)])
    ok = np.abs(q[:, None] / cands[None, :] - 1).min(axis=1) < 0.12
    return ok.mean() >= 0.8


def tech_frame(px, bench):
    """Indicadores diarios precalculados para consultarlos en la fecha de entrada."""
    c, h = px["close"], px["high"].fillna(px["close"])
    t = pd.DataFrame(index=px.index)
    hi252 = h.rolling(252, min_periods=200).max()
    t["pct_from_high"] = (c / hi252 - 1) * 100
    prior = h.shift(10).rolling(242, min_periods=190).max()
    t["breakout"] = h.rolling(10).max() > prior
    t["close_high"] = c > c.shift(1).rolling(251, min_periods=200).max()
    sma50, sma200 = c.rolling(50).mean(), c.rolling(200).mean()
    t["above_sma200"] = c > sma200
    t["trend_up"] = sma50 > sma200
    ret6 = c / c.shift(126) - 1
    b6 = (bench / bench.shift(126) - 1).reindex(t.index, method="ffill")
    t["rs_6m"] = (ret6 - b6) * 100
    v = px["volume"].astype(float)
    t["vol_ratio"] = v.rolling(10).mean() / v.rolling(50).mean()
    t["dollar_vol"] = (c * v).rolling(50).mean()
    return t


def main():
    PX_DIR.mkdir(parents=True, exist_ok=True)
    st, names, periods = load_panel()
    log(f"{len(periods)} trimestres válidos: {periods[0]:%Y-%m} → {periods[-1]:%Y-%m}")
    feat = features(st, periods)
    feat["name"] = feat["cusip"].map(names).fillna("")
    feat = feat[~feat["name"].str.contains(FUND_NAME_RE, regex=True)]
    log(f"{len(feat):,} observaciones cusip-trimestre, {feat['cusip'].nunique():,} CUSIP distintos")

    cmap = map_tickers(sorted(feat["cusip"].unique()))
    feat["ticker"] = feat["cusip"].map(lambda c: (cmap.get(c) or {}).get("ticker"))
    feat["ticker"] = feat["ticker"].where(feat["ticker"].fillna("").str.match(US_TICKER_RE))
    feat["ftype"] = feat["cusip"].map(lambda c: (cmap.get(c) or {}).get("type"))
    feat["ftype2"] = feat["cusip"].map(lambda c: (cmap.get(c) or {}).get("type2"))
    feat = feat[~feat["ftype"].isin(FUND_TYPES) & ~feat["ftype2"].isin(FUND_TYPES)]
    tickers = sorted(set(feat["ticker"].dropna()) | {"SPY"})
    todo = [t for t in tickers if not px_path(t).exists()]
    log(f"Yahoo: {len(tickers)} tickers, {len(todo)} por descargar")
    with ThreadPoolExecutor(6) as ex:
        for i, _ in enumerate(ex.map(yahoo_history, todo), 1):
            if i % 250 == 0:
                log(f"  {i}/{len(todo)}")
    spy = yahoo_history("SPY")["close"]

    implied = st.assign(px=st["value"] / st["shares"].where(st["shares"] > 0)).pivot(index="period", columns="cusip", values="px")
    obs = []
    stats_val = {"sin_ticker": 0, "sin_precio": 0, "no_valida": 0, "ok": 0}
    by_cusip = feat.groupby("cusip")
    for cusip, g in by_cusip:
        tk = g["ticker"].iloc[0]
        if not isinstance(tk, str) or not tk:
            stats_val["sin_ticker"] += 1
            continue
        px = yahoo_history(tk)
        if px is None:
            stats_val["sin_precio"] += 1
            continue
        imp = implied[cusip].dropna() if cusip in implied else pd.Series(dtype=float)
        if not validate(px, imp):
            stats_val["no_valida"] += 1
            continue
        stats_val["ok"] += 1
        px = px[px.index <= imp.index.max() + pd.Timedelta(days=220)]
        tf = tech_frame(px, spy)
        c = px["close"]
        dates = c.index
        for r in g.itertuples():
            sig = r.period + pd.Timedelta(days=LAG_DAYS)
            k = dates.searchsorted(sig, side="right")
            if k >= len(dates) or (dates[k] - sig).days > 7:
                continue
            row = {"cusip": cusip, "period": r.period, "entry": dates[k], **tf.iloc[k - 1].to_dict()}
            for hn, hd in HORIZONS.items():
                if k + hd < len(dates):
                    ret = c.iloc[k + hd] / c.iloc[k] - 1
                    s0 = spy.asof(dates[k]); s1 = spy.asof(dates[k + hd])
                    row[f"ret_{hn}"] = ret * 100
                    row[f"xs_{hn}"] = (ret - (s1 / s0 - 1)) * 100
            obs.append(row)
    log(f"validación de tickers: {stats_val}")
    px_obs = pd.DataFrame(obs)
    df = feat.merge(px_obs, on=["cusip", "period"], how="inner")
    df.to_csv(OUT_DIR / "panel.csv", index=False)
    log(f"panel con precio: {len(df):,} observaciones ({len(df) / len(feat):.0%} del universo 13F)")
    report(df)


def pr(s):
    return s.rank(pct=True)


def score(df):
    g = df.groupby("period")
    out = pd.DataFrame(index=df.index)
    out["p_netas"] = g["d_filers"].transform(pr)
    out["p_pct"] = g["pct_filers"].transform(pr)
    out["p_accel"] = g["accel"].transform(pr)
    out["p_cons"] = df["consistency"] / 4
    out["s13"] = (0.25 * out["p_netas"] + 0.20 * out["p_pct"] + 0.25 * out["p_accel"] + 0.15 * out["p_cons"]) / 0.85 * 100
    prox = (1 + df["pct_from_high"] / 30).clip(0, 1)
    trend = (df["above_sma200"].fillna(False).astype(float) + df["trend_up"].fillna(False).astype(float)) / 2
    live = ((df["breakout"].fillna(False).astype(bool) & (df["pct_from_high"] >= -5)) | df["close_high"].fillna(False).astype(bool))
    out["p_rs"] = g["rs_6m"].transform(pr)
    out["stech"] = (0.35 * prox + 0.25 * trend + 0.30 * out["p_rs"].fillna(0) + 0.10 * live.astype(float)) * 100
    out["score"] = 0.6 * out["s13"] + 0.4 * out["stech"]
    return out


def quintile_table(df, col, ret="xs_3m"):
    d = df.dropna(subset=[col, ret]).copy()
    d["q"] = d.groupby("period")[col].transform(lambda s: np.ceil(s.rank(pct=True, method="first") * 5)).astype(int)
    by_q = d.groupby(["period", "q"])[ret].mean().unstack()
    spread = by_q[5] - by_q[1]
    t = spread.mean() / (spread.std(ddof=1) / np.sqrt(len(spread)))
    return by_q.mean(), spread.mean(), t, (spread > 0).mean(), len(spread)


def topn(df, col, n=20, ret="xs_3m"):
    d = df.dropna(subset=[col, ret])
    top = d.sort_values(col, ascending=False).groupby("period").head(n).groupby("period")[ret].mean()
    allm = d.groupby("period")[ret].mean()
    diff = top - allm
    return top.mean(), allm.mean(), diff.mean(), diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff))), (diff > 0).mean()


def add_flows(df):
    con = sqlite3.connect(DB_PATH)
    try:
        hf = pd.read_sql("SELECT * FROM hist_flows", con)
    except Exception:
        return df
    finally:
        con.close()
    hf["period"] = pd.to_datetime(hf["period"])
    df = df.merge(hf.drop(columns=["entries"]), on=["period", "cusip"], how="left")
    df["conv_rate"] = df["entries_conv"] / df["n_before"]
    df["net_buy"] = (df["buyers"] - df["sellers"]) / (df["buyers"] + df["sellers"]).where(lambda s: s >= 10)
    return df


def report(df):
    df = add_flows(df)
    sc = score(df)
    df = pd.concat([df, sc], axis=1)
    lines = []
    w = lines.append
    w(f"Panel: {len(df):,} obs, {df['period'].nunique()} trimestres ({df['period'].min():%Y-%m} → {df['period'].max():%Y-%m}), "
      f"{df['cusip'].nunique():,} empresas, media {len(df) / df['period'].nunique():.0f} por trimestre")
    for ret in ("xs_3m", "xs_6m"):
        w(f"\n=== Exceso de rentabilidad vs SPY, {ret[3:]} (media de trimestres, %) ===")
        w(f"{'factor':12} {'Q1':>7} {'Q2':>7} {'Q3':>7} {'Q4':>7} {'Q5':>7} {'Q5-Q1':>7} {'t':>6} {'%T>0':>5}  n")
        for col in ("score", "s13", "stech", "p_netas", "p_pct", "p_accel", "p_cons", "pct_from_high", "p_rs", "vol_ratio",
                    "dollar_vol", "conv_rate", "net_buy"):
            if col not in df or df[col].notna().sum() < 1000:
                continue
            q, s, t, hit, n = quintile_table(df, col, ret)
            w(f"{col:12} " + " ".join(f"{q.get(i, np.nan):7.2f}" for i in range(1, 6)) + f" {s:7.2f} {t:6.2f} {hit:5.0%} {n:3d}")
        w(f"\n--- Top 20 por puntuación vs universo ({ret[3:]}) ---")
        for col in ("score", "s13", "stech"):
            a, b, d, t, hit = topn(df, col, 20, ret)
            w(f"{col:8} top20 {a:6.2f}  universo {b:6.2f}  diferencia {d:6.2f}  t {t:5.2f}  gana {hit:4.0%} de trimestres")
    w("\n=== Por tramos (top 20 'score' vs universo, 3m) ===")
    for lo, hi in (("2014", "2017"), ("2018", "2021"), ("2022", "2026")):
        sub = df[(df["period"] >= lo) & (df["period"] < str(int(hi) + 1))]
        if sub["period"].nunique() < 3:
            continue
        a, b, d, t, hit = topn(sub, "score", 20, "xs_3m")
        q, s, tq, hq, n = quintile_table(sub, "score", "xs_3m")
        w(f"{lo}-{hi}: top20 {a:6.2f} univ {b:6.2f} dif {d:6.2f} (t {t:4.2f}, gana {hit:4.0%})  Q5-Q1 {s:6.2f} (t {tq:4.2f})  n={n}")
    text = "\n".join(lines)
    (OUT_DIR / "resultados.txt").write_text(text, encoding="utf-8")
    df.to_csv(OUT_DIR / "panel_puntuado.csv", index=False)
    print(text)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--report":
        report(pd.read_csv(OUT_DIR / "panel.csv", parse_dates=["period", "entry"]))
    else:
        main()
