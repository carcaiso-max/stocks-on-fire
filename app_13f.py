"""App local: buscas un ticker y ves cuántas gestoras 13F lo declaran por trimestre y por año.

Uso:
    python app_13f.py        abre http://127.0.0.1:8613
"""
import json
import os
import re
import sys
import sqlite3
import subprocess
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd

import precios
import valoracion
import insiders
from build_13f import (DATA_DIR, DB_PATH, EMAIL_RE, RAW_DIR, MissingIdentity, list_zip_urls, load_config,
                       sec_identity, update_config)

PORT = int(os.environ.get("FONDOS13F_PORT", 8613))
HERE = Path(__file__).parent
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
TICKERS_FILE = DATA_DIR / "company_tickers.json"
FIGI_CACHE_FILE = DATA_DIR / "figi_cache.json"
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

FUND_TYPES = {"ETP", "Mutual Fund", "Closed-End Fund", "Unit Inv Tr", "Open-End Fund", "ETF", "ETN"}
FUND_NAME_RE = (r"ETFS?|ETN|ISHARES|SPDR|PROSHARES|VANECK|WISDOMTREE|DIREXION|SELECT SECTOR|"
                r"FDS?|FUNDS?|INDEX F|EXCHANGE TRADED|INVESCO QQQ|GLOBAL X|ETF TR")

STOPWORDS = {"INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED", "PLC", "LLC",
             "LP", "HOLDINGS", "HOLDING", "HLDGS", "GROUP", "THE", "NV", "SA", "AG", "SE", "CL", "CLASS",
             "COM", "NEW", "DEL", "DE", "ORD", "SHS", "THE"}


def norm_name(s):
    s = re.sub(r"/[A-Z]{2,3}/?", " ", str(s).upper())
    words = re.sub(r"[^A-Z0-9 ]", " ", s.replace("&", " AND ")).split()
    return " ".join(w for w in words if w not in STOPWORDS)


APP_UA = "StocksOnFire/1.0 (+https://github.com/carcaiso-max/stocks-on-fire)"


def http_json(url, data=None, headers=None, sec=False):
    h = {"User-Agent": sec_identity() if sec else APP_UA}
    h.update(headers or {})
    body = json.dumps(data).encode() if data is not None else None
    if body:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def num(x):
    return f"{x:+,.0f}".replace(",", ".")


def reasons(r):
    """Frases cortas que explican la puntuación."""
    out = [f"{num(r['d_filers'])} gestoras en 1 año ({r['pct_filers']:+.0f} %)"]
    if r["d_recent"] > r["d_prior"] and r["d_recent"] > 0:
        out.append(f"acelerando: {num(r['d_recent'])} en los 2 últimos trimestres frente a {num(r['d_prior'])} en los 2 anteriores")
    elif r["d_recent"] < r["d_prior"]:
        out.append(f"frenando: {num(r['d_recent'])} en los 2 últimos trimestres frente a {num(r['d_prior'])} en los 2 anteriores")
    out.append(f"{int(r['consistency'])}/4 trimestres con más gestoras")
    if r.get("pct_shares") is not None and r["pct_shares"] == r["pct_shares"]:
        out.append(f"acciones en manos de gestoras {r['pct_shares']:+.0f} %")
    if r.get("entries_conv") is not None and r["entries_conv"] == r["entries_conv"]:
        out.append(f"{int(r['entries_conv'])} de {int(r['entries'])} entradas con convicción (>1 % de su cartera)")
    if r.get("buyers") is not None and r["buyers"] == r["buyers"]:
        out.append(f"entre las que ya estaban: {int(r['buyers'])} compran y {int(r['sellers'])} venden")
    if r.get("close_high"):
        out.append("último cierre en máximo de 52 semanas")
    elif r.get("breakout"):
        n = r["days_since_high"]
        cuando = "hoy" if n == 0 else f"hace {n} sesión" if n == 1 else f"hace {n} sesiones"
        estado = "ruptura devuelta" if r["pct_from_high"] < -5 else "nuevo máximo de 52 semanas"
        out.append(f"{estado}: máximo {cuando}, ahora a {r['pct_from_high']:.1f} %".replace(".", ","))
    elif r.get("pct_from_high") is not None:
        out.append(f"a {abs(r['pct_from_high']):.0f} % del máximo de 52 semanas")
    if r.get("fwd_pe") is not None and r["fwd_pe"] == r["fwd_pe"] and r["fwd_pe"] <= 0:
        out.append("los analistas esperan pérdidas el próximo año (sin PER forward)")
    elif r.get("fwd_pe") is not None and r["fwd_pe"] == r["fwd_pe"]:
        txt = f"PER forward {r['fwd_pe']:.1f}"
        if r.get("pe") is not None and r["pe"] == r["pe"]:
            txt += f" (actual {r['pe']:.1f})"
        if r.get("eps_growth") is not None and r["eps_growth"] == r["eps_growth"]:
            txt += f": los analistas esperan un beneficio por acción {r['eps_growth']:+.0f} %"
        out.append(txt.replace(".", ","))
    if r.get("peg") is not None and r["peg"] == r["peg"] and r["peg"] > 0:
        out.append(f"PEG {r['peg']:.2f}".replace(".", ","))
    if r.get("vol_ratio") is not None and r["vol_ratio"] >= 1.5:
        out.append(f"volumen de las últimas 10 sesiones ×{r['vol_ratio']:.1f} su media de 50".replace(".", ","))
    if r.get("above_sma200") is not None:
        out.append("sobre la media de 200 sesiones" if r["above_sma200"] else "bajo la media de 200 sesiones")
    if r.get("rs_6m") is not None:
        out.append(f"{r['rs_6m']:+.0f} puntos frente al S&P 500 a 6 meses")
    return out


def records(df):
    return json.loads(df.to_json(orient="records"))


class Store:
    def __init__(self):
        if not DB_PATH.exists():
            raise FileNotFoundError(DB_PATH)
        con = self.con()
        self.meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        totals = pd.read_sql("SELECT period, SUM(n_filers) AS total FROM stats GROUP BY period ORDER BY period", con)
        totals = totals[pd.to_datetime(totals["period"]).dt.is_quarter_end]
        self.periods = totals.loc[totals["total"] >= 0.25 * totals["total"].max(), "period"].tolist()
        self.last_period = self.periods[-1]
        sec = pd.read_sql("SELECT cusip, name, title_class FROM securities", con)
        last = pd.read_sql("SELECT cusip, n_filers FROM stats WHERE period = ?", con, params=[self.last_period])
        sec = sec.merge(last, on="cusip", how="left").fillna({"n_filers": 0})
        sec["norm"] = sec["name"].map(norm_name)
        self.sec = sec.set_index("cusip")
        con.close()
        self.trends = self.load_trends()
        self.qseries = self.load_qseries()
        self.tickers = self.load_tickers()
        self.name_to_ticker = {}
        for tk, title in self.tickers.items():
            self.name_to_ticker.setdefault(norm_name(title), tk)
        self.figi_cache = self.load_json(FIGI_CACHE_FILE)
        self.figi_lock = threading.Lock()

    def load_qseries(self, n=9):
        """Número de gestoras de los últimos n trimestres completos, una columna por trimestre."""
        ps = self.periods[-n:]
        con = self.con()
        marks = ",".join("?" * len(ps))
        q = pd.read_sql(f"SELECT cusip, period, n_filers FROM stats WHERE period IN ({marks}) AND n_filers >= 10",
                        con, params=ps)
        con.close()
        return q.pivot(index="cusip", columns="period", values="n_filers").reindex(columns=ps).fillna(0)

    def features(self, t):
        """Métricas 13F por CUSIP. Aceleración: gestoras netas de los 2 últimos trimestres menos las de los 2 anteriores."""
        q = self.qseries.reindex(t["cusip"]).fillna(0).to_numpy()
        n0, n2, n4 = q[:, -5], q[:, -3], q[:, -1]
        t = t.copy()
        t["d_recent"] = n4 - n2
        t["d_prior"] = n2 - n0
        t["accel"] = (t["d_recent"] - t["d_prior"]) / np.maximum(n0, 1) * 100
        t["consistency"] = (np.diff(q[:, -5:], axis=1) > 0).sum(axis=1)
        t["spark"] = [list(map(int, r)) for r in q]
        return t

    def stock_universe(self, min_base, horizon=4):
        t = self.trends
        if t.empty:
            return t
        t = t[(t["horizon"] == horizon) & (t["n_before"] >= min_base)]
        return t[~t["name"].fillna("").str.contains(FUND_NAME_RE, regex=True)]

    def tickers_for(self, cusips, stocks_only=True):
        info = self.cusip_info(list(cusips))
        out = {}
        for c in cusips:
            inf = info.get(c, {})
            if stocks_only and (inf.get("type") in FUND_TYPES or inf.get("type2") in FUND_TYPES):
                continue
            out[c] = inf.get("ticker") or self.name_to_ticker.get(norm_name(self.sec["name"].get(c, "")))
        return out

    def suggestions(self, min_base, n, profile, only_high, only_up, min_liq=0.0, max_pe=0.0, max_peg=0.0):
        t = self.stock_universe(min_base)
        if t.empty:
            return {"rows": [], "error": "Falta la tabla de tendencias: ejecuta python build_13f.py --trends-only"}
        t = self.features(t)
        pr = lambda s: s.rank(pct=True).fillna(0)
        t["s13"] = (0.25 * pr(t["d_filers"]) + 0.20 * pr(t["pct_filers"]) + 0.25 * pr(t["accel"])
                    + 0.15 * pr(t["pct_shares"].clip(-100, 300)) + 0.15 * t["consistency"] / 4) * 100
        t = t.sort_values("s13", ascending=False).head(150)
        tick = {c: tk for c, tk in self.tickers_for(t["cusip"].tolist()).items() if tk}
        t = t[t["cusip"].isin(tick)].head(120).copy()
        t["ticker"] = t["cusip"].map(tick)
        hist = precios.many(t["ticker"].tolist() + [precios.BENCH])
        bench = hist.get(precios.BENCH)
        tech = {tk: precios.technicals(df, bench) for tk, df in hist.items() if df is not None and tk != precios.BENCH}
        t = t[t["ticker"].isin(tech)].copy()
        if t.empty:
            return {"rows": [], "error": "No se pudieron descargar precios (¿sin conexión?)."}
        t = t.join(pd.DataFrame([tech[x] for x in t["ticker"]], index=t.index))
        val = valoracion.fetch(t["ticker"].tolist(), with_profile=bool(max_peg))
        t = t.join(pd.DataFrame([val.get(x, {}) for x in t["ticker"]], index=t.index)
                   .reindex(columns=["fwd_pe", "pe", "eps_growth", "peg"]))
        prox = (1 + t["pct_from_high"] / 30).clip(0, 1)
        trend = (t["above_sma200"].fillna(False).astype(float) + t["trend_up"].fillna(False).astype(float)) / 2
        live_breakout = (t["breakout"] & (t["pct_from_high"] >= -5)) | t["close_high"]
        t["stech"] = (0.35 * prox + 0.25 * trend + 0.30 * pr(t["rs_6m"]) + 0.10 * live_breakout.astype(float)) * 100
        w = {"equilibrado": 0.6, "13f": 1.0, "tecnico": 0.4}.get(profile, 0.6)
        t["score"] = w * t["s13"] + (1 - w) * t["stech"]
        if only_high:
            t = t[t["pct_from_high"] >= -5]
        if only_up:
            t = t[t["above_sma200"].fillna(False).astype(bool)]
        if min_liq:
            t = t[t["dollar_vol"].fillna(0) >= min_liq * 1e6]
        if max_pe:
            t = t[t["fwd_pe"].notna() & (t["fwd_pe"] > 0) & (t["fwd_pe"] <= max_pe)]
        if max_peg:
            t = t[t["peg"].notna() & (t["peg"] > 0) & (t["peg"] <= max_peg)]
        t = t.sort_values("score", ascending=False).head(n).copy()
        if not max_peg and len(t):
            pv = valoracion.fetch(t["ticker"].tolist())
            t["peg"] = [pv.get(x, {}).get("peg") for x in t["ticker"]]
        keys = ("cusip", "ticker", "name", "score", "s13", "stech", "n_before", "n_now", "d_filers", "pct_filers",
                "d_recent", "d_prior", "accel", "consistency", "pct_shares", "split", "spark", "close", "pct_from_high",
                "days_since_high", "breakout", "close_high", "at_high", "above_sma200", "trend_up", "ret_6m", "rs_6m",
                "entries", "entries_conv", "buyers", "sellers", "vol_ratio", "dollar_vol", "fwd_pe", "pe", "eps_growth", "peg",
                "reasons")
        rows = []
        for r in t.to_dict("records"):
            r["reasons"] = reasons(r)
            rows.append({k: r.get(k) for k in keys})
        return {"rows": json.loads(pd.DataFrame(rows).to_json(orient="records")) if rows else [],
                "period": self.trends["period"].iloc[0], "price_date": next(iter(tech.values()))["date"],
                "quarters": self.qseries.columns[-5:].tolist()}

    def tech(self, cusip):
        tk = self.tickers_for([cusip], stocks_only=False).get(cusip)
        df = precios.history(tk)
        if df is None:
            return {"ticker": tk, "tech": None, "series": []}
        return {"ticker": tk, "tech": precios.technicals(df, precios.history(precios.BENCH)),
                "series": precios.weekly_series(df), "val": valoracion.fetch([tk]).get(tk, {})}

    def insiders(self, cusip):
        """Compras y ventas de directivos en mercado abierto (formulario 4) de los últimos 24 meses."""
        tk = self.tickers_for([cusip], stocks_only=False).get(cusip)
        if not tk:
            return {"ticker": None, "error": "Sin ticker para esta acción"}
        con = self.con()
        try:
            base = re.sub(r"[./ -]", ".", tk.upper())
            variants = list(dict.fromkeys([base, base.replace(".", "-"), base.replace(".", ""), base.replace(".", "/")]))
            marks = ",".join("?" * len(variants))
            df = pd.read_sql("SELECT filing_date, trans_date, code, owner, role, title, shares, price, value, owned_after, "
                             f"plan_10b51, issuer FROM insider_trades WHERE ticker IN ({marks}) AND filing_date >= ? "
                             "ORDER BY filing_date DESC", con,
                             params=variants + [(pd.Timestamp.today() - pd.DateOffset(months=24)).strftime("%Y-%m-%d")])
        except Exception:
            return {"ticker": tk, "error": "Falta la tabla de directivos: ejecuta python insiders.py"}
        finally:
            con.close()
        if df.empty:
            return {"ticker": tk, "rows": [], "summary": {}, "monthly": []}
        df["filing_date"] = pd.to_datetime(df["filing_date"])
        prev = df["owned_after"] + np.where(df["code"] == "S", df["shares"], -df["shares"])
        df["pct_change"] = np.where(prev > 0, np.where(df["code"] == "S", -df["shares"], df["shares"]) / prev * 100, None)
        people = df[df["role"].isin(["CEO", "CFO", "Directivo", "Consejero"])]

        def side(d, code):
            s = d[d["code"] == code]
            return {"people": int(s["owner"].nunique()), "value": float(s["value"].sum()), "n": int(len(s)),
                    "plan_value": float(s.loc[s["plan_10b51"].astype(bool), "value"].sum())}

        today = pd.Timestamp.today()
        summary = {}
        for label, months in (("12m", 12), ("3m", 3)):
            d = people[people["filing_date"] >= today - pd.DateOffset(months=months)]
            summary[label] = {"buy": side(d, "P"), "sell": side(d, "S")}
        m = people.assign(month=people["filing_date"].dt.to_period("M").astype(str))
        monthly = (m.pivot_table(index="month", columns="code", values="value", aggfunc="sum", fill_value=0)
                   .reindex(columns=["P", "S"], fill_value=0)
                   .reindex(pd.period_range(today - pd.DateOffset(months=23), today, freq="M").astype(str), fill_value=0))
        rows = df.head(40).assign(filing_date=df["filing_date"].dt.strftime("%Y-%m-%d"))
        return {"ticker": tk, "issuer": df["issuer"].iloc[0], "summary": summary,
                "monthly": [{"m": k, "buy": float(v["P"]), "sell": float(v["S"])} for k, v in monthly.iterrows()],
                "rows": json.loads(rows.drop(columns=["issuer"]).to_json(orient="records"))}

    def watch(self, cusips):
        cusips = [c for c in dict.fromkeys(cusips) if c in self.sec.index][:60]
        if not cusips:
            return {"rows": []}
        t = self.trends[(self.trends["horizon"] == 4) & self.trends["cusip"].isin(cusips)]
        missing = [c for c in cusips if c not in set(t["cusip"])]
        if missing:
            t = pd.concat([t, pd.DataFrame({"cusip": missing, "name": self.sec.loc[missing, "name"].to_numpy()})])
        t = self.features(t)
        t["ticker"] = t["cusip"].map(self.tickers_for(cusips, stocks_only=False))
        hist = precios.many([x for x in t["ticker"] if x] + [precios.BENCH])
        bench = hist.get(precios.BENCH)
        val = valoracion.fetch([x for x in t["ticker"] if x])
        keys = ("cusip", "ticker", "name", "n_now", "d_filers", "pct_filers", "d_recent", "d_prior", "accel",
                "consistency", "pct_shares", "spark", "close", "pct_from_high", "days_since_high", "breakout", "close_high",
                "above_sma200", "ret_6m", "rs_6m", "entries_conv", "buyers", "sellers", "vol_ratio", "dollar_vol",
                "fwd_pe", "pe", "eps_growth", "peg")
        rows = []
        for r in t.to_dict("records"):
            df = hist.get(r["ticker"]) if r["ticker"] else None
            if df is not None:
                r.update(precios.technicals(df, bench))
            r.update(val.get(r["ticker"], {}) if r["ticker"] else {})
            rows.append({k: r.get(k) for k in keys})
        order = {c: i for i, c in enumerate(cusips)}
        rows.sort(key=lambda r: order.get(r["cusip"], 0))
        return {"rows": json.loads(pd.DataFrame(rows).to_json(orient="records"))}

    def load_trends(self):
        con = self.con()
        try:
            t = pd.read_sql("SELECT * FROM trends", con)
        except Exception:
            return pd.DataFrame()
        finally:
            con.close()
        t = t.join(self.sec[["name", "title_class"]], on="cusip")
        t["d_filers"] = t["n_now"] - t["n_before"]
        t["pct_filers"] = (t["n_now"] / t["n_before"].where(t["n_before"] > 0) - 1) * 100
        t["pct_shares"] = (t["shares_now"] / t["shares_before_adj"].where(t["shares_before_adj"] > 0) - 1) * 100
        return t

    @staticmethod
    def con():
        return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)

    @staticmethod
    def load_json(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def load_tickers(self):
        fresh = TICKERS_FILE.exists() and time.time() - TICKERS_FILE.stat().st_mtime < 30 * 86400
        if not fresh:
            try:
                TICKERS_FILE.write_text(json.dumps(http_json(TICKERS_URL, sec=True)), encoding="utf-8")
            except (OSError, MissingIdentity) as e:
                print("aviso: no se pudo descargar la lista de tickers de la SEC:", e)
        raw = self.load_json(TICKERS_FILE)
        return {v["ticker"].upper(): v["title"] for v in raw.values()} if raw else {}

    def figis_for_ticker(self, ticker):
        if ticker in self.figi_cache:
            return self.figi_cache[ticker]
        try:
            res = http_json(OPENFIGI_URL, [{"idType": "TICKER", "idValue": ticker, "exchCode": "US"}])
        except OSError as e:
            print("aviso: OpenFIGI no responde:", e)
            return []
        out = []
        for row in (res[0].get("data") or []) if res else []:
            out += [row.get(k) for k in ("figi", "compositeFIGI", "shareClassFIGI") if row.get(k)]
            out.append("NAME:" + row.get("name", ""))
        self.figi_cache[ticker] = out
        try:
            FIGI_CACHE_FILE.write_text(json.dumps(self.figi_cache), encoding="utf-8")
        except OSError:
            pass
        return out

    def save_figi_cache(self):
        try:
            FIGI_CACHE_FILE.write_text(json.dumps(self.figi_cache), encoding="utf-8")
        except OSError:
            pass

    def cusip_info(self, cusips):
        """Ticker y tipo de valor (acción, ETF...) por CUSIP, vía OpenFIGI con caché local."""
        with self.figi_lock:
            missing = [c for c in cusips if "CUSIP:" + c not in self.figi_cache]
            for i in range(0, len(missing), 10):
                batch = missing[i:i + 10]
                try:
                    res = http_json(OPENFIGI_URL, [{"idType": "ID_CUSIP", "idValue": c} for c in batch])
                except OSError as e:
                    print("aviso: OpenFIGI no responde:", e)
                    break
                for c, r in zip(batch, res):
                    rows = r.get("data") or []
                    row = next((x for x in rows if x.get("exchCode") == "US"), rows[0] if rows else {})
                    self.figi_cache["CUSIP:" + c] = {"ticker": row.get("ticker"), "type": row.get("securityType"),
                                                    "type2": row.get("securityType2")}
            if missing:
                self.save_figi_cache()
            return {c: self.figi_cache.get("CUSIP:" + c, {}) for c in cusips}

    def ranking(self, metric, horizon, min_base, n, stocks_only):
        t = self.trends
        if t.empty:
            return {"rows": [], "error": "Falta la tabla de tendencias: ejecuta python build_13f.py --trends-only"}
        t = t[(t["horizon"] == horizon) & (t["n_before"] >= min_base)].copy()
        if stocks_only:
            t = t[~t["name"].fillna("").str.contains(FUND_NAME_RE, regex=True)]
        if metric == "combinado":
            t["score"] = (t["pct_filers"].rank(pct=True) + t["pct_shares"].rank(pct=True)) / 2 * 100
        key = {"netas": "d_filers", "pct": "pct_filers", "acciones": "pct_shares", "combinado": "score"}[metric]
        t = t.dropna(subset=[key]).sort_values(key, ascending=False)
        rows = []
        for i in range(0, min(len(t), 400), 30):
            chunk = t.iloc[i:i + 30]
            info = self.cusip_info(chunk["cusip"].tolist())
            for r in chunk.itertuples():
                inf = info.get(r.cusip, {})
                if stocks_only and (inf.get("type") in FUND_TYPES or inf.get("type2") in FUND_TYPES):
                    continue
                ticker = inf.get("ticker") or self.name_to_ticker.get(norm_name(r.name))
                rows.append({"cusip": r.cusip, "ticker": ticker, "name": r.name, "title_class": r.title_class,
                             "n_before": r.n_before, "n_now": r.n_now, "d_filers": r.d_filers,
                             "pct_filers": r.pct_filers, "entries": r.entries, "exits": r.exits,
                             "pct_shares": r.pct_shares, "split": r.split, "value_now": r.value_now,
                             "entries_conv": getattr(r, "entries_conv", None), "buyers": getattr(r, "buyers", None),
                             "sellers": getattr(r, "sellers", None),
                             "score": getattr(r, "score", None)})
                if len(rows) >= n:
                    break
            if len(rows) >= n:
                break
        val = valoracion.fetch([r["ticker"] for r in rows if r["ticker"]])
        for r in rows:
            v = val.get(r["ticker"], {}) if r["ticker"] else {}
            r["fwd_pe"], r["peg"] = v.get("fwd_pe"), v.get("peg")
        first = t.iloc[0] if len(t) else None
        return {"rows": json.loads(pd.DataFrame(rows).to_json(orient="records")) if rows else [],
                "period": first["period"] if first is not None else None,
                "base_period": first["base_period"] if first is not None else None}

    def candidates(self, cusips, how):
        cusips = [c for c in dict.fromkeys(cusips) if c in self.sec.index]
        df = self.sec.loc[cusips].reset_index().sort_values("n_filers", ascending=False)
        return [dict(cusip=r.cusip, name=r.name, title_class=r.title_class, n_filers=int(r.n_filers), how=how)
                for r in df.head(15).itertuples()]

    def resolve(self, q):
        q = q.strip().upper()
        if not q:
            return [], None
        if re.fullmatch(r"[0-9A-Z]{9}", q) and q in self.sec.index:
            return self.candidates([q], "CUSIP"), None

        is_ticker = re.fullmatch(r"[A-Z0-9.\-/]{1,10}", q)
        title = self.tickers.get(re.sub(r"[./]", "-", q))
        figis = self.figis_for_ticker(re.sub(r"[.\-]", "/", q)) if is_ticker else []
        figi_ids = [f for f in figis if not f.startswith("NAME:")]
        if not title and figis:
            title = next((f[5:] for f in figis if f.startswith("NAME:")), None)
        if figi_ids:
            con = self.con()
            marks = ",".join("?" * len(figi_ids))
            rows = con.execute(f"SELECT cusip, SUM(n) FROM figis WHERE figi IN ({marks}) GROUP BY cusip "
                               f"ORDER BY 2 DESC", figi_ids).fetchall()
            con.close()
            rows = [r for r in rows if r[1] >= 0.1 * rows[0][1]] if rows else rows
            if rows:
                return self.candidates([r[0] for r in rows], "FIGI"), title

        if title:
            n = norm_name(title)
            hit = self.sec[self.sec["norm"] == n]
            if hit.empty and n:
                hit = self.sec[self.sec["norm"].str.startswith(n + " ") | (self.sec["norm"] == n)]
            if not hit.empty:
                return self.candidates(hit.index.tolist(), "nombre"), title

        text = norm_name(q) or q
        hit = self.sec[self.sec["norm"].str.contains(re.escape(text), na=False)]
        return self.candidates(hit.index.tolist(), "texto"), title

    def detail(self, cusip):
        con = self.con()
        st = pd.read_sql("SELECT period, n_filers, n_on_time, shares, value FROM stats WHERE cusip = ? ORDER BY period",
                         con, params=[cusip])
        st = st[st["period"].isin(self.periods)]
        hold = pd.read_sql("SELECT h.cik, m.name, h.shares, h.value FROM holders h LEFT JOIN managers m USING(cik) "
                           "WHERE h.cusip = ? ORDER BY h.value DESC LIMIT 25", con, params=[cusip])
        chg = pd.read_sql("SELECT c.kind, c.cik, m.name, c.shares, c.value, c.period FROM changes c "
                          "LEFT JOIN managers m USING(cik) WHERE c.cusip = ? ORDER BY c.value DESC", con, params=[cusip])
        con.close()

        st["year"] = st["period"].str[:4]
        yearly = st.groupby("year").tail(1).copy()
        yearly["quarter"] = "T" + ((pd.to_datetime(yearly["period"]).dt.month - 1) // 3 + 1).astype(str)
        yearly["delta"] = yearly["n_filers"].diff()
        yearly["pct"] = yearly["n_filers"].pct_change() * 100

        sec = self.sec.loc[cusip]
        return {
            "cusip": cusip, "name": sec["name"], "title_class": sec["title_class"],
            "quarterly": records(st.drop(columns="year")),
            "yearly": records(yearly),
            "holders": records(hold),
            "new": records(chg[chg["kind"] == "nueva"].head(15)),
            "exits": records(chg[chg["kind"] == "salida"].head(15)),
            "n_new": int((chg["kind"] == "nueva").sum()), "n_exits": int((chg["kind"] == "salida").sum()),
            "holders_period": self.meta.get("holder_periods", "").split(",")[-1],
            "built_at": self.meta.get("built_at"), "last_period": self.last_period,
        }


STORE = None


class Updater:
    """Lanza build_13f.py en segundo plano y recarga la base al terminar."""
    LOG = DATA_DIR / "update.log"

    def __init__(self):
        self.proc = None
        self.rc = None
        self.lock = threading.Lock()

    def check(self):
        names = lambda urls: [u.rsplit("/", 1)[1] for u in urls]
        have = {p.name for p in RAW_DIR.glob("*_form13f.zip")}
        new = [n for n in names(list_zip_urls()) if n not in have]
        have_ins = {p.name for p in insiders.RAW_DIR.glob("*_form345.zip")}
        try:
            new += [n for n in names(insiders.list_zip_urls()) if n not in have_ins]
        except OSError:
            pass
        return {"new": new, "local": len(have) + len(have_ins)}

    def start(self):
        sec_identity()
        with self.lock:
            if self.running():
                return False
            self.rc = None
            log = open(self.LOG, "w", encoding="utf-8")
            self.proc = subprocess.Popen([sys.executable, "-u", str(HERE / "build_13f.py")], cwd=HERE,
                                         stdout=log, stderr=subprocess.STDOUT,
                                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            threading.Thread(target=self.wait, args=(log,), daemon=True).start()
            return True

    def wait(self, log):
        global STORE
        rc = self.proc.wait()
        if rc == 0:
            try:
                STORE = Store()
            except BaseException as e:
                log.write(f"error recargando la base: {e}\n")
                rc = -1
        log.close()
        self.rc = rc

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def status(self):
        try:
            lines = [l for l in self.LOG.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
        except OSError:
            lines = []
        return {"running": self.running() or (self.proc is not None and self.rc is None),
                "rc": self.rc, "last": lines[-1] if lines else "",
                "error": "\n".join(lines[-6:]) if self.rc not in (None, 0) else None,
                "has_db": STORE is not None,
                "built_at": STORE.meta.get("built_at") if STORE else None,
                "last_period": STORE.last_period if STORE else None}


UPDATER = Updater()


class AutoUpdater:
    """Con el interruptor encendido, consulta la SEC una vez al día y actualiza si hay ficheros nuevos.
    El estado se guarda en el config.json compartido (load_config/update_config) para no pisar otros ajustes."""
    EVERY = 24 * 3600

    def __init__(self):
        self.lock = threading.Lock()

    def set(self, enabled):
        with self.lock:
            changes = {"auto_update": bool(enabled)}
            if enabled:
                changes.update(last_check=0, last_result=None)
            update_config(**changes)

    def state(self):
        cfg = load_config()
        last = cfg.get("last_check") or 0
        return {"enabled": bool(cfg.get("auto_update")),
                "last_check": time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if last else None,
                "next_check": time.strftime("%Y-%m-%d %H:%M", time.localtime(last + self.EVERY)) if last else None,
                "last_result": cfg.get("last_result")}

    def tick(self):
        with self.lock:
            cfg = load_config()
            if not cfg.get("auto_update") or UPDATER.running():
                return
            if time.time() - (cfg.get("last_check") or 0) < self.EVERY:
                return
            update_config(last_check=time.time())
        retry = None
        try:
            new = UPDATER.check()["new"] if STORE is not None else ["datos iniciales"]
            if new:
                started = UPDATER.start()
                result = f"{len(new)} fichero(s) nuevo(s): actualización {'iniciada' if started else 'ya en marcha'}"
            else:
                result = "sin ficheros nuevos"
        except MissingIdentity:
            result = "falta tu identificación para la SEC"
        except OSError as e:
            result = f"no se pudo consultar la SEC ({e})"
            retry = time.time() - self.EVERY + 3600
        with self.lock:
            update_config(last_result=result, **({"last_check": retry} if retry else {}))
        print(time.strftime("%H:%M"), "actualización automática:", result, flush=True)

    def loop(self):
        time.sleep(60)
        while True:
            try:
                self.tick()
            except Exception as e:
                print("aviso: actualización automática:", e, flush=True)
            time.sleep(15 * 60)


AUTO = AutoUpdater()


class Server(ThreadingHTTPServer):
    allow_reuse_address = False
    allow_reuse_port = False
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        try:
            if url.path == "/":
                self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/identity":
                cfg = load_config()
                self.send(200, {"configured": bool(cfg.get("sec_name") and cfg.get("sec_email")) or bool(os.environ.get("SEC_USER_AGENT")),
                                "name": cfg.get("sec_name"), "email": cfg.get("sec_email")})
            elif url.path == "/api/update/check":
                self.send(200, UPDATER.check())
            elif url.path == "/api/update/status":
                self.send(200, UPDATER.status())
            elif url.path == "/api/auto":
                self.send(200, AUTO.state())
            elif url.path.startswith("/api/") and STORE is None:
                self.send(503, {"error": "Todavía no hay datos: descárgalos primero.", "setup": True})
            elif url.path == "/api/search":
                cands, title = STORE.resolve(qs.get("q", [""])[0])
                self.send(200, {"candidates": cands, "title": title})
            elif url.path == "/api/security":
                cusip = qs.get("cusip", [""])[0].upper()
                if cusip not in STORE.sec.index:
                    self.send(404, {"error": "CUSIP no encontrado"})
                else:
                    self.send(200, STORE.detail(cusip))
            elif url.path == "/api/ranking":
                g = lambda k, d: qs.get(k, [d])[0]
                metric = g("metric", "combinado")
                if metric not in ("netas", "pct", "acciones", "combinado"):
                    metric = "combinado"
                self.send(200, STORE.ranking(metric, int(g("h", "4")), int(g("min", "100")),
                                             min(int(g("n", "10")), 100), g("stocks", "1") == "1"))
            elif url.path == "/api/suggest":
                g = lambda k, d: qs.get(k, [d])[0]
                self.send(200, STORE.suggestions(int(g("min", "100")), min(int(g("n", "20")), 50),
                                                 g("profile", "equilibrado"), g("high", "0") == "1", g("up", "0") == "1",
                                                 float(g("liq", "0")), float(g("pe", "0")),
                                                 float(g("peg", "0"))))
            elif url.path == "/api/tech":
                cusip = qs.get("cusip", [""])[0].upper()
                self.send(200, STORE.tech(cusip) if cusip in STORE.sec.index else {"error": "CUSIP no encontrado"})
            elif url.path == "/api/insiders":
                cusip = qs.get("cusip", [""])[0].upper()
                self.send(200, STORE.insiders(cusip) if cusip in STORE.sec.index else {"error": "CUSIP no encontrado"})
            elif url.path == "/api/watch":
                self.send(200, STORE.watch([c for c in qs.get("cusips", [""])[0].upper().split(",") if c]))
            else:
                self.send(404, {"error": "no encontrado"})
        except MissingIdentity as e:
            self.send(400, {"error": str(e), "identity": True})
        except Exception as e:
            self.send(500, {"error": str(e)})

    def do_POST(self):
        if self.headers.get("X-Fondos13F") != "1":
            return self.send(403, {"error": "prohibido"})
        path = urlparse(self.path).path
        if path == "/api/update/start":
            try:
                started = UPDATER.start()
            except MissingIdentity as e:
                return self.send(400, {"error": str(e), "identity": True})
            self.send(200 if started else 409, {"started": started})
        elif path == "/api/identity":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self.send(400, {"error": "JSON no válido"})
            name, email = str(body.get("name", "")).strip(), str(body.get("email", "")).strip()
            if not name or len(name) > 100 or not EMAIL_RE.match(email):
                return self.send(400, {"error": "Escribe un nombre y un email válidos."})
            update_config(sec_name=name, sec_email=email)
            self.send(200, {"configured": True, "name": name, "email": email})
        elif path == "/api/auto":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self.send(400, {"error": "JSON no válido"})
            AUTO.set(bool(body.get("enabled")))
            self.send(200, AUTO.state())
        else:
            self.send(404, {"error": "no encontrado"})


if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}"
    try:
        srv = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        print("El servidor ya estaba encendido; abriendo el navegador.")
        webbrowser.open(url)
        sys.exit(0)
    try:
        STORE = Store()
        print(f"Stocks on fire en {url}  (último trimestre completo: {STORE.last_period})  Ctrl+C para salir")
    except FileNotFoundError:
        STORE = None
        print(f"Stocks on fire en {url}  (sin datos todavía: descárgalos desde la app)  Ctrl+C para salir")
    threading.Thread(target=AUTO.loop, daemon=True).start()
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    srv.serve_forever()
