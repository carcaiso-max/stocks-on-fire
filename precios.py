"""Precios diarios (Yahoo Finance, caché diaria en disco) e indicadores técnicos."""
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from build_13f import DATA_DIR

PRICES_DIR = DATA_DIR / "precios"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range=5y&interval=1d"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
MAX_AGE = 6 * 3600
BENCH = "SPY"


def yahoo_symbol(ticker):
    return ticker.upper().replace(".", "-").replace("/", "-")


def history(ticker):
    """DataFrame diario (date, open, high, low, close, volume) de 5 años, o None."""
    if not ticker:
        return None
    sym = yahoo_symbol(ticker)
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    path = PRICES_DIR / f"{sym}.json"
    raw = None
    if path.exists() and time.time() - path.stat().st_mtime < MAX_AGE:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raw = None
    if raw is None:
        try:
            req = urllib.request.Request(CHART_URL.format(sym), headers={"User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                res = json.loads(r.read())["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            raw = {"t": res["timestamp"], "o": q["open"], "h": q["high"], "l": q["low"], "c": q["close"], "v": q["volume"]}
            path.write_text(json.dumps(raw), encoding="utf-8")
        except Exception:
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
            else:
                return None
    df = pd.DataFrame({"date": pd.to_datetime(raw["t"], unit="s").normalize(), "open": raw["o"], "high": raw["h"],
                       "low": raw["l"], "close": raw["c"], "volume": raw["v"]}).dropna(subset=["close"])
    df = df.drop_duplicates("date", keep="last").reset_index(drop=True)
    return df if len(df) > 30 else None


def _ret(c, n):
    return (c.iloc[-1] / c.iloc[-1 - n] - 1) * 100 if len(c) > n else None


def technicals(df, bench=None):
    """Indicadores al último cierre."""
    c, h = df["close"], df["high"]
    last = c.iloc[-1]
    win = min(252, len(df))
    hi52 = h.iloc[-win:].max()
    lo52 = df["low"].iloc[-win:].min()
    prior_hi = h.iloc[-win - 10:-10].max() if len(df) > win + 10 else h.iloc[:-10].max()
    hi_idx = int(h.iloc[-win:].to_numpy().argmax())
    days_since_high = win - 1 - hi_idx
    sma50 = c.rolling(50).mean().iloc[-1] if len(c) >= 50 else None
    sma200 = c.rolling(200).mean().iloc[-1] if len(c) >= 200 else None
    vol50 = df["volume"].iloc[-50:].mean()
    vol10 = df["volume"].iloc[-10:].mean()
    out = {
        "date": df["date"].iloc[-1].strftime("%Y-%m-%d"), "close": float(last),
        "high52": float(hi52), "low52": float(lo52), "pct_from_high": float((last / hi52 - 1) * 100),
        "days_since_high": days_since_high,
        "breakout": bool(h.iloc[-10:].max() > prior_hi) if prior_hi == prior_hi else False,
        "at_high": bool(last >= 0.98 * hi52),
        "close_high": bool(last > c.iloc[-win:-1].max()),
        "sma50": None if sma50 is None else float(sma50), "sma200": None if sma200 is None else float(sma200),
        "above_sma200": None if sma200 is None else bool(last > sma200),
        "trend_up": None if sma50 is None or sma200 is None else bool(sma50 > sma200),
        "ret_3m": _ret(c, 63), "ret_6m": _ret(c, 126), "ret_12m": _ret(c, 252),
        "vol_ratio": float(vol10 / vol50) if vol50 else None,
        "dollar_vol": float((c.iloc[-50:] * df["volume"].iloc[-50:]).mean()),
    }
    if bench is not None:
        b6 = _ret(bench["close"], 126)
        out["rs_6m"] = None if out["ret_6m"] is None or b6 is None else out["ret_6m"] - b6
    return {k: (None if isinstance(v, float) and not np.isfinite(v) else v) for k, v in out.items()}


def weekly_series(df, years=5):
    w = df.set_index("date")["close"].resample("W-FRI").last().dropna()
    w = w[w.index >= w.index[-1] - pd.DateOffset(years=years)]
    return [{"d": d.strftime("%Y-%m-%d"), "c": round(float(v), 4)} for d, v in w.items()]


def many(tickers, workers=8):
    """{ticker: DataFrame|None} descargando en paralelo."""
    tickers = [t for t in dict.fromkeys(tickers) if t]
    with ThreadPoolExecutor(workers) as ex:
        return dict(zip(tickers, ex.map(history, tickers)))
