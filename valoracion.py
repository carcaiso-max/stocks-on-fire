"""Valoración con estimaciones de analistas (Yahoo Finance, uso personal): PER forward, PER actual,
beneficio por acción esperado frente al actual, PEG y sector. Caché local de 12 horas.
Solo informativo: no hay histórico gratuito de estimaciones, así que no se puede validar con un backtest."""
import http.cookiejar
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from build_13f import DATA_DIR

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
CACHE_FILE = DATA_DIR / "valoracion.json"
QUOTE_TTL = 12 * 3600
PROFILE_TTL = 12 * 3600
QUOTE_URL = "https://query2.finance.yahoo.com/v7/finance/quote?symbols={}&crumb={}"
SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{}?modules=defaultKeyStatistics,summaryProfile&crumb={}"

_lock = threading.Lock()
_session = {"opener": None, "crumb": None, "t": 0}
_cache = None


def _symbol(ticker):
    return ticker.upper().replace(".", "-").replace("/", "-")


def _load():
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save():
    try:
        CACHE_FILE.write_text(json.dumps(_cache), encoding="utf-8")
    except OSError:
        pass


def _open(url, retry=True):
    """Petición con cookie y 'crumb' de Yahoo; se renuevan cada hora o si caducan."""
    with _lock:
        if _session["opener"] is None or time.time() - _session["t"] > 3600:
            cj = http.cookiejar.CookieJar()
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            op.addheaders = [("User-Agent", UA)]
            try:
                op.open("https://fc.yahoo.com", timeout=15)
            except urllib.error.HTTPError:
                pass
            crumb = op.open("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=15).read().decode()
            _session.update(opener=op, crumb=crumb, t=time.time())
        op, crumb = _session["opener"], _session["crumb"]
    try:
        return json.loads(op.open(url.replace("{crumb}", crumb), timeout=20).read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and retry:
            _session["t"] = 0
            return _open(url, retry=False)
        raise


def _num(x):
    if isinstance(x, dict):
        x = x.get("raw")
    return x if isinstance(x, (int, float)) else None


def _profile(sym):
    try:
        res = _open(SUMMARY_URL.format(sym, "{crumb}"))["quoteSummary"]["result"][0]
    except Exception:
        return {}
    prof, ks = res.get("summaryProfile") or {}, res.get("defaultKeyStatistics") or {}
    return {"sector": prof.get("sector"), "industry": prof.get("industry"), "peg": _num(ks.get("pegRatio"))}


def fetch(tickers, with_profile=True):
    """{ticker: datos de valoración} usando la caché; descarga lo que falte o haya caducado."""
    cache = _load()
    now = time.time()
    syms = {t: _symbol(t) for t in dict.fromkeys(tickers) if t}
    stale = [s for s in syms.values() if now - cache.get(s, {}).get("t", 0) > QUOTE_TTL]
    for i in range(0, len(stale), 50):
        batch = stale[i:i + 50]
        try:
            res = _open(QUOTE_URL.format(",".join(batch), "{crumb}"))["quoteResponse"]["result"]
        except Exception as e:
            print("aviso: Yahoo (valoración) no responde:", e, flush=True)
            break
        got = {q["symbol"]: q for q in res}
        for s in batch:
            q = got.get(s, {})
            old = cache.get(s, {})
            cache[s] = {**old, "t": now, "fwd_pe": _num(q.get("forwardPE")), "pe": _num(q.get("trailingPE")),
                        "eps_fwd": _num(q.get("epsForward")), "eps_ttm": _num(q.get("epsTrailingTwelveMonths")),
                        "market_cap": _num(q.get("marketCap"))}
    if with_profile:
        need = [s for s in syms.values() if s in cache and now - cache[s].get("tp", 0) > PROFILE_TTL]
        with ThreadPoolExecutor(6) as ex:
            for s, prof in zip(need, ex.map(_profile, need)):
                if prof:
                    cache[s].update(prof, tp=now)
    _save()
    out = {}
    for t, s in syms.items():
        v = dict(cache.get(s, {}))
        v.pop("t", None), v.pop("tp", None)
        if v.get("fwd_pe") is not None and v["fwd_pe"] <= 0:
            v["peg"] = None
        if v.get("eps_fwd") is not None and v.get("eps_ttm") and v["eps_ttm"] > 0:
            v["eps_growth"] = (v["eps_fwd"] / v["eps_ttm"] - 1) * 100
        out[t] = v
    return out
