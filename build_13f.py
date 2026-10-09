"""Descarga los Form 13F Data Sets de la SEC y construye una base SQLite compacta:
número de gestoras 13F que declaran cada CUSIP en cada trimestre.

Uso:
    python build_13f.py            descarga lo que falte y reconstruye la base
    python build_13f.py --no-download   solo reconstruye con los zips ya bajados
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

LISTING_URL = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
DATA_DIR = Path(os.environ.get("FONDOS13F_DIR", Path.home() / "fondos13f"))
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "fondos13f.sqlite"
CONFIG_PATH = DATA_DIR / "config.json"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


class MissingIdentity(RuntimeError):
    pass


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update_config(**changes):
    cfg = load_config()
    cfg.update(changes)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return cfg


def sec_identity():
    """Nombre y email con los que la SEC pide identificarse en las descargas automáticas."""
    env = os.environ.get("SEC_USER_AGENT")
    if env:
        return env
    cfg = load_config()
    if cfg.get("sec_name") and cfg.get("sec_email"):
        return f"{cfg['sec_name']} {cfg['sec_email']}"
    raise MissingIdentity("Falta tu identificación para la SEC (nombre y email). Configúrala en la app "
                          "o ejecuta este script desde una consola para que te la pida.")


def ask_identity():
    print("La SEC pide que quien descarga sus datos se identifique con su nombre y email.")
    print("Se guardan solo en este ordenador y únicamente se envían a sec.gov.")
    name = input("Nombre: ").strip()
    email = input("Email: ").strip()
    if not name or not EMAIL_RE.match(email):
        sys.exit("Nombre o email no válidos.")
    update_config(sec_name=name, sec_email=email)


VALUE_IN_DOLLARS_FROM = pd.Timestamp("2023-01-03")
FILING_DEADLINE_DAYS = 45
HOLDER_PERIODS = 2
TOP_HOLDERS = 100
CHUNK = 1_000_000


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": sec_identity()})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def list_zip_urls():
    html = http_get(LISTING_URL).decode("utf-8", "replace")
    urls = re.findall(r'href="([^"]+_form13f\.zip)"', html)
    urls = [u if u.startswith("http") else "https://www.sec.gov" + u for u in urls]
    return list(dict.fromkeys(urls))


def download_all():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    urls = list_zip_urls()
    log(f"{len(urls)} ficheros en la SEC")
    for url in urls:
        dest = RAW_DIR / url.rsplit("/", 1)[1]
        if dest.exists() and zipfile.is_zipfile(dest):
            continue
        log(f"descargando {dest.name}")
        data = http_get(url)
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
        time.sleep(0.5)


def member(zf, name):
    return next(n for n in zf.namelist() if n.rsplit("/", 1)[-1].upper() == name.upper())


def read_tsv(zf, name, usecols=None, **kw):
    name = member(zf, name)
    with zf.open(name) as f:
        cols = None
        if usecols is not None:
            header = f.readline().decode("utf-8", "replace").rstrip("\r\n").split("\t")
            cols = [c for c in usecols if c in header]
        with zf.open(name) as f2:
            return pd.read_csv(f2, sep="\t", dtype=str, usecols=cols, quoting=3,
                               encoding_errors="replace", keep_default_na=False, **kw)


def parse_date(s):
    return pd.to_datetime(s, format="%d-%b-%Y", errors="coerce")


def load_metadata(zips):
    frames = []
    for z in zips:
        with zipfile.ZipFile(z) as zf:
            sub = read_tsv(zf, "SUBMISSION.tsv")
            cov = read_tsv(zf, "COVERPAGE.tsv", usecols=["ACCESSION_NUMBER", "AMENDMENTTYPE", "FILINGMANAGER_NAME"])
        sub = sub[sub["SUBMISSIONTYPE"].isin(["13F-HR", "13F-HR/A"])]
        m = sub.merge(cov, on="ACCESSION_NUMBER", how="left")
        m["zip"] = z.name
        frames.append(m)
    meta = pd.concat(frames, ignore_index=True).drop_duplicates("ACCESSION_NUMBER")
    meta["cik"] = pd.to_numeric(meta["CIK"], errors="coerce").astype("Int64")
    meta["filing_date"] = parse_date(meta["FILING_DATE"])
    meta["period"] = parse_date(meta["PERIODOFREPORT"])
    meta = meta.dropna(subset=["cik", "filing_date", "period"])
    meta["AMENDMENTTYPE"] = meta["AMENDMENTTYPE"].fillna("").str.upper()
    meta["is_new_holdings"] = (meta["SUBMISSIONTYPE"] == "13F-HR/A") & meta["AMENDMENTTYPE"].str.contains("NEW")
    return meta


def select_accessions(meta):
    """Por (gestora, periodo): el último informe completo (original o restatement)
    más las enmiendas de 'new holdings' posteriores a él."""
    base = meta[~meta["is_new_holdings"]].sort_values(["cik", "period", "filing_date", "ACCESSION_NUMBER"])
    base_last = base.groupby(["cik", "period"]).tail(1)[["cik", "period", "filing_date"]]
    base_last = base_last.rename(columns={"filing_date": "base_date"})
    nh = meta[meta["is_new_holdings"]].merge(base_last, on=["cik", "period"])
    nh = nh[nh["filing_date"] >= nh["base_date"]]
    chosen = pd.concat([base.groupby(["cik", "period"]).tail(1), nh[meta.columns]], ignore_index=True)

    first = meta.groupby(["cik", "period"])["filing_date"].min().rename("first_filing").reset_index()
    first["on_time"] = (first["first_filing"] - first["period"]).dt.days <= FILING_DEADLINE_DAYS
    chosen = chosen.merge(first, on=["cik", "period"])
    return chosen


def clean_cusip(s):
    s = s.str.strip().str.upper().str.replace(r"[^0-9A-Z]", "", regex=True)
    short = s.str.len().isin([7, 8])
    s = s.where(~short, s.str.zfill(9))
    return s


def fix_value_scale(df):
    """Muchas gestoras declaran en dólares cuando tocaba en miles (o al revés): se corrige la fila
    cuyo precio implícito se aleja más de 50x de la mediana de ese CUSIP en el trimestre."""
    price = (df["value"] / df["shares"]).where(df["shares"] > 0)
    med = price.groupby([df["period"], df["cusip"]]).transform("median")
    ratio = price / med
    value = df["value"].where(~(ratio > 50), df["value"] / 1000)
    return value.where(~(ratio < 1 / 50), value * 1000)


def process_zip(z, acc):
    """Devuelve posiciones agregadas por (period, cusip, cik) y conteos de nombres/FIGI."""
    acc_z = acc[acc["zip"] == z.name].set_index("ACCESSION_NUMBER")
    if acc_z.empty:
        return None, None, None
    cols = ["ACCESSION_NUMBER", "NAMEOFISSUER", "TITLEOFCLASS", "CUSIP", "FIGI", "VALUE",
            "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL"]
    pos, names, figis = [], [], []
    with zipfile.ZipFile(z) as zf:
        info_name = member(zf, "INFOTABLE.tsv")
        with zf.open(info_name) as f:
            header = f.readline().decode("utf-8", "replace").rstrip("\r\n").split("\t")
        use = [c for c in cols if c in header]
        with zf.open(info_name) as f:
            reader = pd.read_csv(f, sep="\t", dtype=str, usecols=use, quoting=3, chunksize=CHUNK,
                                 encoding_errors="replace", keep_default_na=False)
            for ch in reader:
                ch = ch[ch["ACCESSION_NUMBER"].isin(acc_z.index)]
                if "PUTCALL" in ch:
                    ch = ch[ch["PUTCALL"].str.strip() == ""]
                ch = ch[ch["SSHPRNAMTTYPE"].str.strip().str.upper() == "SH"]
                if ch.empty:
                    continue
                ch = ch.assign(CUSIP=clean_cusip(ch["CUSIP"]))
                ch = ch[ch["CUSIP"].str.len() == 9]
                info = acc_z.loc[ch["ACCESSION_NUMBER"], ["cik", "period", "filing_date"]].reset_index(drop=True)
                ch = ch.reset_index(drop=True)
                value = pd.to_numeric(ch["VALUE"], errors="coerce").fillna(0.0)
                value = value.where(info["filing_date"] >= VALUE_IN_DOLLARS_FROM, value * 1000)
                shares = pd.to_numeric(ch["SSHPRNAMT"], errors="coerce").fillna(0.0)
                df = pd.DataFrame({"period": info["period"], "cusip": ch["CUSIP"], "cik": info["cik"],
                                   "shares": shares, "value": value})
                df["value"] = fix_value_scale(df)
                pos.append(df.groupby(["period", "cusip", "cik"], as_index=False)[["shares", "value"]].sum())
                nm = ch["NAMEOFISSUER"].str.strip().str.upper()
                cl = ch["TITLEOFCLASS"].str.strip().str.upper()
                names.append(pd.DataFrame({"cusip": ch["CUSIP"], "name": nm, "title_class": cl})
                             .value_counts().rename("n").reset_index())
                if "FIGI" in ch:
                    fg = ch.loc[ch["FIGI"].str.strip() != "", ["CUSIP", "FIGI"]]
                    if not fg.empty:
                        figis.append(pd.DataFrame({"cusip": fg["CUSIP"], "figi": fg["FIGI"].str.strip().str.upper()})
                                     .value_counts().rename("n").reset_index())
    if not pos:
        return None, None, None
    pos = pd.concat(pos).groupby(["period", "cusip", "cik"], as_index=False)[["shares", "value"]].sum()
    names = pd.concat(names).groupby(["cusip", "name", "title_class"], as_index=False)["n"].sum()
    figis = pd.concat(figis).groupby(["cusip", "figi"], as_index=False)["n"].sum() if figis else None
    return pos, names, figis


def complete_periods(meta, n):
    """Últimos n trimestres con cobertura completa (≥ 85% de las gestoras del mayor trimestre reciente)."""
    counts = meta.groupby("period")["cik"].nunique().sort_index()
    recent_max = counts.tail(8).max()
    good = counts[counts >= 0.85 * recent_max]
    return list(good.index[-n:])


def build(db_path=DB_PATH):
    zips = sorted(RAW_DIR.glob("*_form13f.zip"))
    if not zips:
        sys.exit("No hay zips descargados. Ejecuta sin --no-download.")
    log(f"leyendo metadatos de {len(zips)} ficheros")
    meta = load_metadata(zips)
    acc = select_accessions(meta)
    log(f"{len(acc):,} informes seleccionados, {acc['cik'].nunique():,} gestoras")
    latest, prev, yago = trend_periods(meta)
    holder_periods = {p for p in (latest, prev, yago) if p is not None}
    on_time = acc.drop_duplicates(["cik", "period"]).set_index(["cik", "period"])["on_time"]

    stats, names, figis, holders = [], [], [], []
    for i, z in enumerate(zips, 1):
        t0 = time.time()
        pos, nm, fg = process_zip(z, acc)
        if pos is None:
            continue
        pos["on_time"] = on_time.reindex(pd.MultiIndex.from_frame(pos[["cik", "period"]])).fillna(False).to_numpy()
        names.append(nm)
        if fg is not None:
            figis.append(fg)
        hp = pos[pos["period"].isin(holder_periods)]
        if not hp.empty:
            holders.append(hp)
        stats.append(aggregate_stats(pos))
        log(f"[{i}/{len(zips)}] {z.name}: {len(pos):,} posiciones ({time.time() - t0:.0f}s)")

    log("agregando")
    st = (pd.concat(stats).groupby(["period", "cusip"], as_index=False)
          [["n_filers", "n_on_time", "shares", "value"]].sum())
    nm = pd.concat(names).groupby(["cusip", "name", "title_class"], as_index=False)["n"].sum()
    nm = nm.sort_values("n", ascending=False).drop_duplicates("cusip")
    fg = (pd.concat(figis).groupby(["cusip", "figi"], as_index=False)["n"].sum()
          if figis else pd.DataFrame(columns=["cusip", "figi", "n"]))
    managers = (meta.sort_values("filing_date").drop_duplicates("cik", keep="last")[["cik", "FILINGMANAGER_NAME"]]
                .rename(columns={"FILINGMANAGER_NAME": "name"}))

    hold = pd.concat(holders) if holders else pd.DataFrame(columns=["period", "cusip", "cik", "shares", "value"])
    hold = hold.groupby(["period", "cusip", "cik"], as_index=False)[["shares", "value"]].sum()
    hp = [p for p in (prev, latest) if p is not None]
    changes = holder_changes(hold, hp)
    top = (hold[hold["period"] == hp[-1]].sort_values("value", ascending=False)
           .groupby("cusip").head(TOP_HOLDERS)) if hp else hold
    trends = all_trends(hold, latest, prev, yago)

    log("escribiendo base de datos")
    tmp = Path(str(db_path) + ".tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    st.assign(period=st["period"].pipe(pd.to_datetime).dt.strftime("%Y-%m-%d")).to_sql("stats", con, index=False)
    nm.to_sql("securities", con, index=False)
    fg.to_sql("figis", con, index=False)
    managers.to_sql("managers", con, index=False)
    top.assign(period=top["period"].pipe(pd.to_datetime).dt.strftime("%Y-%m-%d")).to_sql("holders", con, index=False)
    changes.assign(period=changes["period"].pipe(pd.to_datetime).dt.strftime("%Y-%m-%d")).to_sql("changes", con, index=False)
    write_trends(con, trends)
    pd.DataFrame({"key": ["built_at", "zips", "holder_periods"],
                  "value": [time.strftime("%Y-%m-%d %H:%M"), str(len(zips)),
                            ",".join(p.strftime("%Y-%m-%d") for p in hp)]}).to_sql("meta", con, index=False)
    con.executescript("""
        CREATE INDEX ix_stats ON stats(cusip, period);
        CREATE INDEX ix_sec_name ON securities(name);
        CREATE INDEX ix_figi ON figis(figi);
        CREATE INDEX ix_hold ON holders(cusip);
        CREATE INDEX ix_chg ON changes(cusip);
        CREATE INDEX ix_mgr ON managers(cik);
    """)
    con.commit()
    con.close()
    tmp.replace(db_path)
    log(f"listo: {db_path} ({db_path.stat().st_size / 1e6:.0f} MB)")


def trend_periods(meta):
    """Último trimestre completo, el anterior y el mismo trimestre un año antes."""
    ps = complete_periods(meta, 8)
    if not ps:
        return None, None, None
    latest = ps[-1]
    prev = ps[-2] if len(ps) > 1 else None
    target = latest - pd.DateOffset(years=1) + pd.offsets.QuarterEnd(0)
    yago = next((p for p in ps if p == target), None)
    return latest, prev, yago


SPLIT_RATIOS = [1.5, 2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40, 50]
CONVICTION_WEIGHT = 0.01
TRADE_BAND = 0.05


def detect_splits(both):
    """Un split hace que muchas gestoras que no operan tengan exactamente k veces más acciones.
    Para cada CUSIP se elige el ratio k con más gestoras a ±1% (y al menos el doble que el siguiente)."""
    r = both["shares_b"] / both["shares_a"]
    cands = [1.0] + [c for k in SPLIT_RATIOS for c in (k, 1 / k)]
    hits = pd.DataFrame({k: (r / k - 1).abs() < 0.01 for k in cands}).groupby(both["cusip"].to_numpy()).mean()
    size = both.groupby("cusip").size()
    best = hits.idxmax(axis=1)
    ranked = np.sort(hits.to_numpy(), axis=1)
    first, second = ranked[:, -1], ranked[:, -2]
    ok = (first >= 0.02) & (first >= 2 * second) & (size.reindex(hits.index).to_numpy() >= 10)
    return best.where(ok, 1.0).astype(float).rename("split")


def compute_trends(hold, latest, base, horizon, min_filers=20):
    a = hold[hold["period"] == base][["cusip", "cik", "shares"]]
    b = hold[hold["period"] == latest][["cusip", "cik", "shares", "value"]]
    aum = b.groupby("cik")["value"].sum()
    b = b.assign(weight=b["value"] / b["cik"].map(aum))
    m = a.merge(b, on=["cusip", "cik"], how="outer", suffixes=("_a", "_b"))
    both = m.dropna(subset=["shares_a", "shares_b"])
    both = both[(both["shares_a"] > 0) & (both["shares_b"] > 0)]
    split = detect_splits(both)
    m["entry"] = m["shares_a"].isna() & m["shares_b"].notna()
    m["exit"] = m["shares_b"].isna() & m["shares_a"].notna()
    m["entry_conv"] = m["entry"] & (m["weight"] >= CONVICTION_WEIGHT)
    ratio = m["shares_b"] / (m["shares_a"] * m["cusip"].map(split).fillna(1.0))
    m["buyer"] = ratio > 1 + TRADE_BAND
    m["seller"] = ratio < 1 - TRADE_BAND
    g = m.groupby("cusip")
    t = pd.DataFrame({
        "n_before": g["shares_a"].count(), "n_now": g["shares_b"].count(),
        "entries": g["entry"].sum(), "exits": g["exit"].sum(), "entries_conv": g["entry_conv"].sum(),
        "buyers": g["buyer"].sum(), "sellers": g["seller"].sum(),
        "shares_before": g["shares_a"].sum(), "shares_now": g["shares_b"].sum(), "value_now": g["value"].sum(),
    })
    t = t[(t["n_now"] >= min_filers) | (t["n_before"] >= min_filers)]
    t = t.join(split).fillna({"split": 1.0})
    t["shares_before_adj"] = t["shares_before"] * t["split"]
    t["horizon"] = horizon
    t["period"] = latest
    t["base_period"] = base
    return t.reset_index()


def all_trends(hold, latest, prev, yago):
    out = [compute_trends(hold, latest, base, h) for base, h in ((yago, 4), (prev, 1)) if base is not None]
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def write_trends(con, trends):
    if trends.empty:
        return
    t = trends.copy()
    for c in ("period", "base_period"):
        t[c] = pd.to_datetime(t[c]).dt.strftime("%Y-%m-%d")
    t.to_sql("trends", con, index=False, if_exists="replace")
    con.execute("CREATE INDEX IF NOT EXISTS ix_trends ON trends(horizon, cusip)")


def build_trends_only(db_path=DB_PATH):
    """Recalcula solo la tabla de tendencias procesando los ficheros con los trimestres necesarios."""
    zips = sorted(RAW_DIR.glob("*_form13f.zip"))
    meta = load_metadata(zips)
    acc = select_accessions(meta)
    latest, prev, yago = trend_periods(meta)
    periods = {p for p in (latest, prev, yago) if p is not None}
    need = sorted(set(acc.loc[acc["period"].isin(periods), "zip"]))
    log(f"tendencias {latest:%Y-%m-%d}: procesando {len(need)} ficheros")
    acc = acc[acc["period"].isin(periods)]
    holders = []
    for name in need:
        pos, _, _ = process_zip(RAW_DIR / name, acc)
        if pos is not None:
            holders.append(pos[pos["period"].isin(periods)])
        log(f"  {name}")
    hold = pd.concat(holders).groupby(["period", "cusip", "cik"], as_index=False)[["shares", "value"]].sum()
    trends = all_trends(hold, latest, prev, yago)
    con = sqlite3.connect(db_path)
    write_trends(con, trends)
    con.commit()
    con.close()
    log(f"listo: {len(trends):,} filas de tendencias")


def aggregate_stats(pos):
    return (pos.groupby(["period", "cusip"])
            .agg(n_filers=("cik", "nunique"), n_on_time=("on_time", "sum"),
                 shares=("shares", "sum"), value=("value", "sum"))
            .reset_index())


def holder_changes(hold, periods):
    """Gestoras que entran y salen entre los dos últimos trimestres completos."""
    cols = ["period", "cusip", "cik", "kind", "shares", "value"]
    if len(periods) < 2:
        return pd.DataFrame(columns=cols)
    prev, cur = periods[-2], periods[-1]
    a = hold[hold["period"] == prev][["cusip", "cik", "shares", "value"]]
    b = hold[hold["period"] == cur][["cusip", "cik", "shares", "value"]]
    m = a.merge(b, on=["cusip", "cik"], how="outer", suffixes=("_prev", "_cur"), indicator=True)
    new = m[m["_merge"] == "right_only"].assign(kind="nueva", shares=lambda d: d["shares_cur"], value=lambda d: d["value_cur"])
    out = m[m["_merge"] == "left_only"].assign(kind="salida", shares=lambda d: d["shares_prev"], value=lambda d: d["value_prev"])
    res = pd.concat([new, out])[["cusip", "cik", "kind", "shares", "value"]].assign(period=cur)
    return res[cols]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--trends-only", action="store_true")
    args = ap.parse_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not args.no_download and not args.trends_only:
        try:
            sec_identity()
        except MissingIdentity:
            if not sys.stdin or not sys.stdin.isatty():
                raise
            ask_identity()
    if args.trends_only:
        build_trends_only()
        sys.exit(0)
    if not args.no_download:
        download_all()
    build()
    import insiders
    try:
        if not args.no_download:
            insiders.download_all()
        insiders.build()
    except Exception as e:
        log(f"aviso: no se pudo actualizar la tabla de directivos: {e}")
    import fundamentales
    try:
        if not args.no_download:
            fundamentales.download()
        fundamentales.build()
    except Exception as e:
        log(f"aviso: no se pudo actualizar la tabla de fundamentales: {e}")
