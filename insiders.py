"""Compras y ventas de directivos en mercado abierto (formulario 4 de la SEC), 2006 → hoy.

Descarga los Insider Transactions Data Sets trimestrales y guarda en la base la tabla `insider_trades`
(códigos P = compra y S = venta en mercado abierto; solo formularios 4 originales).

Uso:
    python insiders.py               descarga lo que falte y reconstruye la tabla
    python insiders.py --no-download
"""
import argparse
import re
import sqlite3
import time
import urllib.request
import zipfile

import pandas as pd

from build_13f import DATA_DIR, DB_PATH, log, member, sec_identity

LISTING_URL = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
INS_DIR = DATA_DIR / "insiders"
RAW_DIR = INS_DIR / "raw"


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": sec_identity()})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def list_zip_urls():
    html = http_get(LISTING_URL).decode("utf-8", "replace")
    urls = list(dict.fromkeys(re.findall(r'href="([^"]+_form345\.zip)"', html)))
    return [u if u.startswith("http") else "https://www.sec.gov" + u for u in urls]


def download_all():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    urls = list_zip_urls()
    log(f"{len(urls)} ficheros de directivos en la SEC")
    for url in urls:
        dest = RAW_DIR / url.rsplit("/", 1)[1]
        if dest.exists() and zipfile.is_zipfile(dest):
            continue
        log(f"descargando {dest.name}")
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(http_get(url))
        tmp.replace(dest)
        time.sleep(0.3)


def read(zf, name, cols):
    with zf.open(member(zf, name)) as f:
        header = f.readline().decode("utf-8", "replace").rstrip("\r\n").split("\t")
    use = [c for c in cols if c in header]
    with zf.open(member(zf, name)) as f:
        return pd.read_csv(f, sep="\t", dtype=str, usecols=use, quoting=3, keep_default_na=False,
                           encoding_errors="replace")


def role(rel, title):
    """Cargo simplificado: CEO, CFO, otro directivo, consejero o accionista >10 %."""
    t = title.upper()
    r = rel.upper()
    if re.search(r"\bCEO\b|CHIEF EXECUTIVE|PRESIDENT AND CEO", t):
        return "CEO"
    if re.search(r"\bCFO\b|CHIEF FINANCIAL", t):
        return "CFO"
    if "OFFICER" in r:
        return "Directivo"
    if "DIRECTOR" in r:
        return "Consejero"
    if "TENPERCENT" in r.replace(" ", "").replace("%", "PERCENT") or "10" in r:
        return "Accionista >10 %"
    return "Otro"


def parse_zip(z):
    with zipfile.ZipFile(z) as zf:
        sub = read(zf, "SUBMISSION.tsv", ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERCIK", "ISSUERNAME",
                                          "ISSUERTRADINGSYMBOL", "AFF10B5ONE"])
        own = read(zf, "REPORTINGOWNER.tsv", ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP",
                                              "RPTOWNER_TITLE"])
        tr = read(zf, "NONDERIV_TRANS.tsv", ["ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES",
                                             "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD", "SHRS_OWND_FOLWNG_TRANS",
                                             "DIRECT_INDIRECT_OWNERSHIP"])
    sub = sub[sub["DOCUMENT_TYPE"] == "4"]
    tr = tr[tr["TRANS_CODE"].isin(["P", "S"])]
    own = own.drop_duplicates("ACCESSION_NUMBER")
    df = tr.merge(sub, on="ACCESSION_NUMBER").merge(own, on="ACCESSION_NUMBER", how="left")
    if df.empty:
        return df
    out = pd.DataFrame({
        "accession": df["ACCESSION_NUMBER"],
        "filing_date": pd.to_datetime(df["FILING_DATE"], format="%d-%b-%Y", errors="coerce"),
        "trans_date": pd.to_datetime(df["TRANS_DATE"], format="%d-%b-%Y", errors="coerce"),
        "code": df["TRANS_CODE"],
        "issuer_cik": pd.to_numeric(df["ISSUERCIK"], errors="coerce"),
        "issuer": df["ISSUERNAME"].str.strip(),
        "ticker": df["ISSUERTRADINGSYMBOL"].str.strip().str.upper(),
        "owner_cik": pd.to_numeric(df["RPTOWNERCIK"], errors="coerce"),
        "owner": df["RPTOWNERNAME"].str.strip(),
        "role": [role(r, t) for r, t in zip(df["RPTOWNER_RELATIONSHIP"].fillna(""), df["RPTOWNER_TITLE"].fillna(""))],
        "title": df["RPTOWNER_TITLE"].fillna("").str.strip(),
        "shares": pd.to_numeric(df["TRANS_SHARES"], errors="coerce"),
        "price": pd.to_numeric(df["TRANS_PRICEPERSHARE"], errors="coerce"),
        "owned_after": pd.to_numeric(df["SHRS_OWND_FOLWNG_TRANS"], errors="coerce"),
        "direct": df["DIRECT_INDIRECT_OWNERSHIP"].str.strip(),
        "plan_10b51": (df["AFF10B5ONE"] if "AFF10B5ONE" in df else pd.Series("", index=df.index))
        .fillna("").str.strip().str.lower().isin(["1", "true"]),
    })
    out = out.dropna(subset=["filing_date", "trans_date", "shares", "price"])
    out = out[(out["shares"] > 0) & (out["price"] > 0)]
    out["value"] = out["shares"] * out["price"]
    return out


def build():
    zips = sorted(RAW_DIR.glob("*_form345.zip"))
    frames = []
    for z in zips:
        f = parse_zip(z)
        frames.append(f)
        log(f"{z.name}: {(f['code'] == 'P').sum():,} compras, {(f['code'] == 'S').sum():,} ventas")
    df = pd.concat(frames, ignore_index=True).drop_duplicates()
    for c in ("filing_date", "trans_date"):
        df[c] = df[c].dt.strftime("%Y-%m-%d")
    con = sqlite3.connect(DB_PATH)
    df.to_sql("insider_trades", con, index=False, if_exists="replace")
    con.executescript("""
        CREATE INDEX IF NOT EXISTS ix_ins_ticker ON insider_trades(ticker, filing_date);
        CREATE INDEX IF NOT EXISTS ix_ins_filing ON insider_trades(filing_date, code);
    """)
    con.commit()
    con.close()
    log(f"listo: {len(df):,} operaciones ({(df['code'] == 'P').sum():,} compras)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-download", action="store_true")
    args = ap.parse_args()
    if not args.no_download:
        download_all()
    build()
