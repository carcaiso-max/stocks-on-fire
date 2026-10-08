"""Flujos históricos por acción y trimestre, punto a punto (solo informes presentados en plazo):
entradas, entradas con convicción (≥1 % de la cartera de la gestora) y compradores/vendedores
entre las gestoras que repiten, comparando cada trimestre con el mismo de un año antes.

Escribe la tabla `hist_flows` en la base. Uso: python historial_flujos.py
"""
import sqlite3
import time

import pandas as pd

from build_13f import (CONVICTION_WEIGHT, DB_PATH, FILING_DEADLINE_DAYS, RAW_DIR, TRADE_BAND, detect_splits,
                       load_metadata, log, process_zip)


def pit_accessions(meta):
    """Por (gestora, trimestre): el último informe completo presentado en plazo y sus 'new holdings' en plazo."""
    on_time = meta[(meta["filing_date"] - meta["period"]).dt.days <= FILING_DEADLINE_DAYS]
    base = on_time[~on_time["is_new_holdings"]].sort_values(["cik", "period", "filing_date", "ACCESSION_NUMBER"])
    last = base.groupby(["cik", "period"]).tail(1)
    nh = on_time[on_time["is_new_holdings"]].merge(
        last[["cik", "period", "filing_date"]].rename(columns={"filing_date": "base_date"}), on=["cik", "period"])
    nh = nh[nh["filing_date"] >= nh["base_date"]][on_time.columns]
    return pd.concat([last, nh], ignore_index=True)


def flows(cur, old):
    aum = cur.groupby("cik")["value"].sum()
    cur = cur.assign(weight=cur["value"] / cur["cik"].map(aum))
    m = old[["cusip", "cik", "shares"]].merge(cur[["cusip", "cik", "shares", "weight"]], on=["cusip", "cik"],
                                              how="outer", suffixes=("_a", "_b"))
    both = m.dropna(subset=["shares_a", "shares_b"])
    both = both[(both["shares_a"] > 0) & (both["shares_b"] > 0)]
    split = detect_splits(both)
    m["entry"] = m["shares_a"].isna() & m["shares_b"].notna()
    m["entry_conv"] = m["entry"] & (m["weight"] >= CONVICTION_WEIGHT)
    ratio = m["shares_b"] / (m["shares_a"] * m["cusip"].map(split).fillna(1.0))
    m["buyer"] = ratio > 1 + TRADE_BAND
    m["seller"] = ratio < 1 - TRADE_BAND
    g = m.groupby("cusip")
    out = pd.DataFrame({"n_pit": g["shares_b"].count(), "entries": g["entry"].sum(), "entries_conv": g["entry_conv"].sum(),
                        "buyers": g["buyer"].sum(), "sellers": g["seller"].sum()})
    return out[out["n_pit"] >= 20].reset_index()


def main():
    zips = sorted(RAW_DIR.glob("*_form13f.zip"))
    meta = load_metadata(zips)
    acc = pit_accessions(meta)
    counts = acc.groupby("period")["cik"].nunique().sort_index()
    local = counts.rolling(9, center=True, min_periods=1).median()
    periods = sorted(counts[(counts >= 0.5 * local) & (counts >= 1000) & counts.index.is_quarter_end].index)
    acc = acc[acc["period"].isin(periods)]
    zip_of = acc.groupby("period")["zip"].agg(lambda s: sorted(set(s)))
    log(f"{len(periods)} trimestres, {len(acc):,} informes en plazo")

    by_zip = {}
    for p in periods:
        for z in zip_of[p]:
            by_zip.setdefault(z, []).append(p)
    order = sorted(by_zip, key=lambda z: min(by_zip[z]))
    pending = {}
    holdings = {}
    results = []
    for z in order:
        t0 = time.time()
        pos, _, _ = process_zip(RAW_DIR / z, acc[acc["zip"] == z])
        if pos is None:
            continue
        for p, g in pos.groupby("period"):
            pending.setdefault(p, []).append(g[["cusip", "cik", "shares", "value"]])
        done = [p for p in pending if all(zz in order[:order.index(z) + 1] for zz in zip_of.get(p, []))]
        for p in sorted(done):
            h = pd.concat(pending.pop(p)).groupby(["cusip", "cik"], as_index=False)[["shares", "value"]].sum()
            holdings[p] = h
            p4 = p - pd.DateOffset(years=1) + pd.offsets.QuarterEnd(0)
            if p4 in holdings:
                f = flows(h, holdings[p4])
                f["period"] = p.strftime("%Y-%m-%d")
                results.append(f)
            for old in [q for q in holdings if q < p - pd.DateOffset(months=13)]:
                del holdings[old]
        log(f"{z}: {len(pos):,} posiciones ({time.time() - t0:.0f}s)")

    res = pd.concat(results, ignore_index=True)
    con = sqlite3.connect(DB_PATH)
    res.to_sql("hist_flows", con, index=False, if_exists="replace")
    con.execute("CREATE INDEX IF NOT EXISTS ix_hist_flows ON hist_flows(period, cusip)")
    con.commit()
    con.close()
    log(f"listo: {len(res):,} filas, {res['period'].nunique()} trimestres")


if __name__ == "__main__":
    main()
