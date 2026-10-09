"""Fundamentales de la SEC (XBRL, fichero companyfacts.zip): ventas, beneficio neto, flujo de caja operativo,
inversión en activo fijo, patrimonio neto y acciones en circulación, con la fecha en que se publicó cada cifra.

Las cifras de cuenta de resultados y flujos se convierten a 12 meses móviles (TTM):
    TTM = último año fiscal completo + acumulado del año en curso − acumulado del mismo periodo del año anterior.
Cada TTM lleva la fecha de publicación del informe que lo completa, así que se puede usar sin mirar al futuro.

Escribe en la base las tablas `fund_facts` (cik, metric, end, filed, value) y `cik_tickers` (ticker, cik).
Uso: python fundamentales.py [--no-download]
"""
import argparse
import json
import os
import sqlite3
import time
import urllib.request
import zipfile
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from build_13f import DATA_DIR, DB_PATH, log, sec_identity

URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
FUND_DIR = DATA_DIR / "fundamentales"
ZIP_PATH = FUND_DIR / "companyfacts.zip"
FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "40-F", "10-KT"}

FLOW_TAGS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueGoodsNet"],
    "net_income": ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
}
INSTANT_TAGS = {"equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]}


def download():
    FUND_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ZIP_PATH.with_suffix(".part")
    req = urllib.request.Request(URL, headers={"User-Agent": sec_identity()})
    log("descargando companyfacts.zip (~1,4 GB)")
    with urllib.request.urlopen(req, timeout=600) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    tmp.replace(ZIP_PATH)


def _frame(entries):
    df = pd.DataFrame(entries)
    if df.empty or "form" not in df:
        return pd.DataFrame()
    df = df[df["form"].isin(FORMS)]
    for c in ("start", "end", "filed"):
        if c in df:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    return df.dropna(subset=["end", "filed", "val"])


def ttm_series(entries):
    """(end, filed, value) en 12 meses móviles a partir de cifras anuales y acumuladas del año."""
    df = _frame(entries)
    if df.empty or "start" not in df:
        return pd.DataFrame(columns=["end", "filed", "value"])
    df = df.dropna(subset=["start"]).sort_values("filed").drop_duplicates(["start", "end"], keep="first")
    df["dur"] = (df["end"] - df["start"]).dt.days
    annual = df[df["dur"].between(350, 380)][["end", "filed", "val"]]
    ytd = df[df["dur"].between(80, 349)][["start", "end", "filed", "val", "dur"]].copy()
    parts = [annual.rename(columns={"val": "value"})]
    if not ytd.empty and not annual.empty:
        ytd["q"] = (ytd["dur"] / 91).round().astype(int)
        fy = annual.rename(columns={"end": "fy_end", "filed": "fy_filed", "val": "fy_val"}).sort_values("fy_end")
        m = pd.merge_asof(ytd.sort_values("start"), fy, left_on="start", right_on="fy_end",
                          direction="backward", tolerance=pd.Timedelta(days=7))
        m["prev_end"] = m["end"] - pd.DateOffset(years=1)
        prev = ytd.rename(columns={"end": "p_end", "filed": "p_filed", "val": "p_val"})[["p_end", "p_filed", "p_val", "q"]]
        m = pd.merge_asof(m.sort_values("prev_end"), prev.sort_values("p_end"), left_on="prev_end", right_on="p_end",
                          by="q", direction="nearest", tolerance=pd.Timedelta(days=7))
        m = m.dropna(subset=["fy_val", "p_val"])
        if not m.empty:
            parts.append(pd.DataFrame({"end": m["end"], "filed": m[["filed", "fy_filed", "p_filed"]].max(axis=1),
                                       "value": m["fy_val"] + m["val"] - m["p_val"]}))
    out = pd.concat(parts, ignore_index=True)
    return out.sort_values("filed").drop_duplicates("end", keep="first")


def instant_series(entries, sum_classes=False):
    df = _frame(entries)
    if df.empty:
        return pd.DataFrame(columns=["end", "filed", "value"])
    if sum_classes:
        df = df.groupby(["accn", "end"], as_index=False).agg(filed=("filed", "min"), val=("val", "sum"))
    df = df.sort_values("filed").drop_duplicates("end", keep="first")
    return df.rename(columns={"val": "value"})[["end", "filed", "value"]]


def merge_tags(series_list):
    """Une las series de varias etiquetas XBRL (las empresas cambian de etiqueta con los años); gana la primera."""
    parts = [s.assign(prio=i) for i, s in enumerate(series_list) if not s.empty]
    if not parts:
        return pd.DataFrame(columns=["end", "filed", "value"])
    return pd.concat(parts).sort_values(["end", "prio"]).drop_duplicates("end", keep="first").drop(columns="prio")


def company_rows(data):
    facts = data.get("facts", {})
    gaap = facts.get("us-gaap", {})
    out = []
    for metric, tags in FLOW_TAGS.items():
        s = merge_tags([ttm_series(gaap.get(t, {}).get("units", {}).get("USD", [])) for t in tags])
        out.append(s.assign(metric=metric))
    for metric, tags in INSTANT_TAGS.items():
        s = merge_tags([instant_series(gaap.get(t, {}).get("units", {}).get("USD", [])) for t in tags])
        out.append(s.assign(metric=metric))
    dei = facts.get("dei", {}).get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
    out.append(instant_series(dei, sum_classes=True).assign(metric="shares"))
    df = pd.concat([o for o in out if not o.empty], ignore_index=True) if any(not o.empty for o in out) else pd.DataFrame()
    return df


def cik_ticker_map():
    """Tickers actuales (lista de la SEC) más los históricos que aparecen en los formularios de directivos."""
    rows = []
    try:
        raw = json.loads((DATA_DIR / "company_tickers.json").read_text(encoding="utf-8"))
        rows += [(v["ticker"].upper(), int(v["cik_str"]), "9999-12-31") for v in raw.values()]
    except (OSError, ValueError):
        pass
    con = sqlite3.connect(DB_PATH)
    try:
        ins = pd.read_sql("SELECT ticker, issuer_cik AS cik, MAX(filing_date) AS last_seen FROM insider_trades "
                          "WHERE ticker IS NOT NULL AND issuer_cik IS NOT NULL GROUP BY ticker, issuer_cik", con)
        rows += [(t.upper(), int(c), d) for t, c, d in ins.itertuples(index=False)]
    except Exception:
        pass
    finally:
        con.close()
    m = pd.DataFrame(rows, columns=["ticker", "cik", "last_seen"])
    m["ticker"] = m["ticker"].str.replace("-", ".").str.replace("/", ".")
    return m.sort_values("last_seen").drop_duplicates("ticker", keep="last")


def _process_chunk(names):
    frames = []
    with zipfile.ZipFile(ZIP_PATH) as zf:
        for n in names:
            try:
                data = json.loads(zf.read(n))
            except ValueError:
                continue
            rows = company_rows(data)
            if not rows.empty:
                frames.append(rows.assign(cik=int(data.get("cik", 0))))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def build():
    with zipfile.ZipFile(ZIP_PATH) as zf:
        names = [n for n in zf.namelist() if n.endswith(".json")]
    log(f"{len(names):,} empresas en companyfacts")
    chunks = [names[i:i + 500] for i in range(0, len(names), 500)]
    frames = []
    with ProcessPoolExecutor(max(1, min(8, (os.cpu_count() or 2) - 1))) as ex:
        for i, f in enumerate(ex.map(_process_chunk, chunks), 1):
            frames.append(f)
            if i % 5 == 0:
                log(f"  {min(i * 500, len(names)):,}/{len(names):,}")
    df = pd.concat(frames, ignore_index=True)
    for c in ("end", "filed"):
        df[c] = pd.to_datetime(df[c]).dt.strftime("%Y-%m-%d")
    con = sqlite3.connect(DB_PATH)
    df[["cik", "metric", "end", "filed", "value"]].to_sql("fund_facts", con, index=False, if_exists="replace")
    con.execute("CREATE INDEX IF NOT EXISTS ix_fund ON fund_facts(cik, metric, filed)")
    cik_ticker_map().to_sql("cik_tickers", con, index=False, if_exists="replace")
    con.execute("CREATE INDEX IF NOT EXISTS ix_cik_tickers ON cik_tickers(ticker)")
    con.commit()
    con.close()
    log(f"listo: {len(df):,} cifras de {df['cik'].nunique():,} empresas")


def snapshot(facts, asof, mcap=None):
    """Métricas conocidas en la fecha `asof` a partir de las filas de fund_facts de una empresa.
    `mcap`: capitalización en esa fecha (si se pasa, se calculan los ratios de valoración)."""
    f = facts[(facts["filed"] <= asof) & (facts["end"] <= asof)]
    out = {}

    def last(metric):
        s = f[f["metric"] == metric].sort_values(["end", "filed"])
        return s.iloc[-1] if len(s) else None

    def year_ago(metric, end):
        s = f[(f["metric"] == metric) & ((f["end"] - (end - pd.DateOffset(years=1))).abs().dt.days <= 20)]
        return s.sort_values("filed").iloc[-1]["value"] if len(s) else None

    for m in ("revenue", "net_income", "ocf", "capex", "equity", "shares"):
        r = last(m)
        out[m] = None if r is None else float(r["value"])
        if m in ("revenue", "net_income") and r is not None:
            out[m + "_end"] = r["end"]
            prev = year_ago(m, r["end"])
            out[m + "_growth"] = ((r["value"] / prev - 1) * 100) if prev and prev > 0 else None
    ni, rev = out.get("net_income"), out.get("revenue")
    out["net_margin"] = ni / rev * 100 if ni is not None and rev else None
    fcf = out["ocf"] - (out["capex"] or 0) if out.get("ocf") is not None else None
    out["fcf"] = fcf
    if mcap and mcap > 0:
        out["ep"] = ni / mcap * 100 if ni is not None else None
        out["fcf_yield"] = fcf / mcap * 100 if fcf is not None else None
        out["bm"] = out["equity"] / mcap if out.get("equity") is not None else None
        pe = mcap / ni if ni and ni > 0 else None
        out["pe_sec"] = pe
        g = out.get("net_income_growth")
        out["peg_hist"] = pe / g if pe and g and g > 0 else None
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-download", action="store_true")
    args = ap.parse_args()
    if not args.no_download:
        download()
    build()
