"""
FMP HTTP helpers, universe construction, and cached price loads.

Beginner notes:
- We cache each ticker's prices under `outputs/cache/prices/<SYMBOL>.parquet`
  (with CSV fallback compatibility) so reruns are fast and gentle on the API.
- Multi-symbol pulls use ``get_price_histories_long`` (parallel cache-aware requests).
  FMP does not offer a single bulk endpoint for dividend-adjusted *history*; batch quote
  and EOD-bulk APIs cover other use cases.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover
    tqdm = None  # type: ignore


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def load_api_key() -> str:
    """Load `FMP_API_KEY` from `.env` next to this project."""
    load_dotenv(_repo_root() / ".env")
    key = os.getenv("FMP_API_KEY")
    if key is None:
        print("Error: FMP_API_KEY is missing from environment/.env", file=sys.stderr)
        sys.exit(1)
    key = key.strip()
    if not key:
        print("Error: FMP_API_KEY is empty", file=sys.stderr)
        sys.exit(1)
    return key


def create_http_session() -> requests.Session:
    retry = Retry(
        total=config.HTTP_MAX_RETRIES,
        backoff_factor=config.HTTP_BACKOFF_S,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s = requests.Session()
    s.mount("https://", adapter)
    s.headers.update({"User-Agent": "FMP-sector-model/1.0"})
    return s


# Back-compat for internal calls
def _build_session() -> requests.Session:
    return create_http_session()


def _fmp_get(session: requests.Session, api_key: str, path: str, **params: str) -> list | dict:
    url = f"{config.FMP_BASE_URL}/{path.lstrip('/')}"
    q = {"apikey": api_key, **params}
    r = session.get(url, params=q, timeout=config.HTTP_TIMEOUT_S)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("Error Message"):
        raise RuntimeError(str(data["Error Message"]))
    return data


def _progress(it, desc: str):
    if tqdm is not None:
        return tqdm(it, desc=desc)
    print(f"[start] {desc}", flush=True)
    return it


def _done_progress(desc: str):
    if tqdm is None:
        print(f"[done] {desc}", flush=True)


def _looks_like_warrant_or_right(symbol: str) -> bool:
    s = symbol.upper()
    if ".WS" in s or ".WT" in s:
        return True
    if s.endswith("-R") or s.endswith("+") or s.endswith("-W"):
        return True
    return False


def _looks_like_warrant_industry(industry: str | float | None) -> bool:
    if industry is None or (isinstance(industry, float) and pd.isna(industry)):
        return False
    return "warrant" in str(industry).lower()


def _looks_like_preferred_or_unit(symbol: str) -> bool:
    """Filter preferreds, units, SPAC oddities, etc. (heuristic)."""
    s = str(symbol).upper().strip()
    if not s:
        return True
    if "^" in s:
        return True
    if s.endswith("-P") or s.endswith("-PR") or s.endswith(".PR"):
        return True
    if s.endswith("-U") or s.endswith(".U") or s.endswith("-UN"):
        return True
    if " PR" in s or ".RT" in s:
        return True
    return False


def get_stock_universe(session: requests.Session, api_key: str, *, top_n: int | None = None) -> pd.DataFrame:
    """
    Pull a US stock universe from FMP's company screener, then apply:

    - US major exchanges only (exclude OTC / pink)
    - exclude ETFs / funds / obvious warrants & rights

    No minimum market cap or volume on the screener call; results are capped
    with ``top_n`` by descending market cap after filters.
    """
    print("[universe] Downloading company screener (this can take a few seconds)...", flush=True)
    raw = _fmp_get(
        session,
        api_key,
        "company-screener",
        isActivelyTrading="true",
        country="US",
        limit="5000",
    )
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("Company screener returned no rows.")

    df = pd.DataFrame(raw)

    # --- pandas-side filters (defense in depth) ---
    if "isEtf" in df.columns:
        df = df[~df["isEtf"].astype(bool)]
    if "isFund" in df.columns:
        df = df[~df["isFund"].astype(bool)]

    if "exchangeShortName" in df.columns:
        df = df[df["exchangeShortName"].isin(config.US_MAJOR_EXCHANGES)]

    if "country" in df.columns:
        df = df[df["country"].astype(str).str.upper() == "US"]

    if "symbol" in df.columns:
        df = df[~df["symbol"].astype(str).map(_looks_like_warrant_or_right)]
    if "industry" in df.columns:
        df = df[~df["industry"].map(_looks_like_warrant_industry)]

    if "sector" in df.columns:
        df = df[df["sector"].isin(config.RANKING_VALID_SECTORS)]

    if "symbol" in df.columns:
        df = df[~df["symbol"].astype(str).map(_looks_like_preferred_or_unit)]

    # Numeric hygiene
    df["marketCap"] = pd.to_numeric(df.get("marketCap"), errors="coerce")
    df["volume"] = pd.to_numeric(df.get("volume"), errors="coerce")
    df = df.dropna(subset=["symbol", "sector", "marketCap"])

    cap_n = int(top_n) if top_n is not None else int(config.STOCK_UNIVERSE_TOP_N)
    df = df.sort_values("marketCap", ascending=False).head(cap_n)

    print(f"[universe] Using top {len(df)} US stocks by market cap (no min cap/volume on screener).", flush=True)
    return df.reset_index(drop=True)


def _cache_path(symbol: str) -> Path:
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = symbol.upper().replace("/", "_")
    return config.CACHE_DIR / f"{safe}.csv"


def _parquet_cache_path(symbol: str) -> Path:
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = symbol.upper().replace("/", "_")
    return config.CACHE_DIR / f"{safe}.parquet"


def _read_price_cache(symbol: str) -> pd.DataFrame:
    """Prefer parquet cache; fall back to legacy CSV cache."""
    pq = _parquet_cache_path(symbol)
    if pq.is_file():
        try:
            df = pd.read_parquet(pq)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"], errors="coerce")
            return df
        except Exception:
            pass

    csv = _cache_path(symbol)
    if csv.is_file():
        try:
            return pd.read_csv(csv, parse_dates=["date"])
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


def _price_cache_file_mtime_ns(symbol: str) -> int:
    """Latest on-disk mtime for ``symbol`` (parquet preferred, else CSV)."""
    pq = _parquet_cache_path(symbol)
    if pq.is_file():
        try:
            return int(pq.stat().st_mtime_ns)
        except OSError:
            pass
    csv = _cache_path(symbol)
    if csv.is_file():
        try:
            return int(csv.stat().st_mtime_ns)
        except OSError:
            pass
    return 0


def price_cache_fingerprint(symbols: Iterable[str]) -> str:
    """
    Stable token derived from price cache file mtimes.

    Intended as a Streamlit ``@st.cache_data`` argument: when any symbol’s parquet/CSV
    is rewritten (new bars, merge, refresh), the fingerprint changes and cached bundles
    recompute without waiting for TTL.
    """
    seen: set[str] = set()
    pairs: list[tuple[str, int]] = []
    for raw in symbols:
        s = str(raw).upper().strip()
        if not s or s in seen:
            continue
        seen.add(s)
        pairs.append((s, _price_cache_file_mtime_ns(s)))
    pairs.sort(key=lambda x: x[0])
    payload = "\n".join(f"{sym}:{mt}" for sym, mt in pairs)
    if not payload:
        return "noprices"
    return hashlib.sha256(payload.encode()).hexdigest()[:40]


def profile_bulk_cache_fingerprint() -> str:
    """Mtime token for the on-disk profile-bulk concat (dispersion universe source)."""
    path = Path(getattr(config, "PROFILE_BULK_CACHE_PATH", ""))
    if path.is_file():
        try:
            return str(path.stat().st_mtime_ns)
        except OSError:
            pass
    return "0"


def dispersion_settings_fingerprint() -> str:
    """Knobs that affect ``build_dispersion_universe`` / price window (cache bust when config edits)."""
    keys = (
        "DISPERSION_MIN_MARKET_CAP",
        "DISPERSION_MIN_AVG_VOLUME",
        "DISPERSION_MIN_PRICE",
        "DISPERSION_MAX_UNIVERSE_SIZE",
        "DISPERSION_PRICE_HISTORY_CAL_DAYS",
        "DISPERSION_CHART_LOOKBACK_DAYS",
        "DISPERSION_DMA_WARMUP_DAYS",
        "DISPERSION_RETURN_TRADING_DAYS",
        "DISPERSION_CORR_TRADING_DAYS",
        "DISPERSION_TS_STRIDE",
    )
    parts: list[str] = []
    for k in keys:
        parts.append(f"{k}={getattr(config, k, '')}")
    return "|".join(parts)


def dispersion_bundle_cache_revision(sector: str, universe_symbols: list[str]) -> str:
    """Single string for ``@st.cache_data`` — changes when profile, settings, or member price files change."""
    sec = str(sector).strip()
    return "|".join(
        (
            profile_bulk_cache_fingerprint(),
            dispersion_settings_fingerprint(),
            sec,
            price_cache_fingerprint(universe_symbols),
        )
    )


def _fundamentals_cache_path(symbol: str) -> Path:
    config.FUNDAMENTALS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = symbol.upper().replace("/", "_")
    return config.FUNDAMENTALS_CACHE_DIR / f"{safe}.json"


def get_profile_snapshot(
    session: requests.Session,
    api_key: str,
    symbol: str,
    *,
    force_refresh: bool = False,
) -> dict:
    """Minimal company profile (used for averageVolume). Cached JSON."""
    sym = symbol.upper()
    path = _fundamentals_cache_path(f"{sym}__profile")
    if path.is_file() and not force_refresh:
        return json.loads(path.read_text(encoding="utf-8"))

    raw = _fmp_get(session, api_key, "profile", symbol=sym)
    if not isinstance(raw, list) or not raw:
        return {}
    row = raw[0]
    path.write_text(json.dumps(row, default=str), encoding="utf-8")
    return row


def enrich_universe_average_volume(
    session: requests.Session,
    api_key: str,
    universe: pd.DataFrame,
    *,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Add `averageVolume` from `/stable/profile` (screener `volume` is not the same metric).

    Does not filter on volume; all input rows are returned with `averageVolume` filled when available.
    """
    out_rows: list[dict] = []
    iterator = _progress(universe.to_dict("records"), desc="Profile (avg volume)")
    for row in iterator:
        sym = str(row.get("symbol", "")).upper()
        if not sym:
            continue
        try:
            prof = get_profile_snapshot(session, api_key, sym, force_refresh=force_refresh)
            av = prof.get("averageVolume")
            row = dict(row)
            row["averageVolume"] = float(av) if av is not None and av == av else float("nan")
        except Exception as e:
            print(f"  warning: profile {sym}: {e}", file=sys.stderr)
            row = dict(row)
            row["averageVolume"] = float("nan")
        out_rows.append(row)
    _done_progress("Profile (avg volume)")
    df = pd.DataFrame(out_rows)
    df["averageVolume"] = pd.to_numeric(df["averageVolume"], errors="coerce")
    return df.reset_index(drop=True)


def _read_fundamentals_cache(path: Path) -> dict[str, float | str | None] | None:
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, float | str | None] = {}
    for k, v in raw.items():
        if v is None:
            out[k] = None
        elif isinstance(v, str):
            out[k] = v
        else:
            try:
                fv = float(v)
                out[k] = None if math.isnan(fv) or math.isinf(fv) else fv
            except (TypeError, ValueError):
                out[k] = None
    return out


def get_fundamentals(
    session: requests.Session,
    api_key: str,
    symbol: str,
    *,
    force_refresh: bool = False,
) -> dict[str, float | None]:
    """
    Pull quality/value inputs from FMP (cached JSON per ticker).

    Includes raw fields used to derive FCF yield and EV/EBITDA when the API omits them:
    - FCF yield prefers `freeCashFlowYieldTTM`, else freeCashFlowTTM / marketCap.
    - EV/EBITDA prefers `evToEBITDATTM`, else enterpriseValueTTM / ebitdaTTM.

    Skips failed HTTP calls per endpoint; missing keys stay None (caller imputes by sector).
    """
    sym = symbol.upper()
    path = _fundamentals_cache_path(sym)
    if not force_refresh:
        cached = _read_fundamentals_cache(path)
        if cached is not None and cached.get("_schema") == "multifactor_v1":
            return {k: v for k, v in cached.items() if k != "_schema"}

    bundle: dict[str, float | None | str] = {
        "_schema": "multifactor_v1",
        "roic": None,
        "roe": None,
        "operating_margin": None,
        "gross_margin": None,
        "revenue_growth": None,
        "fcf": None,
        "market_cap": None,
        "debt_to_equity": None,
        "ev": None,
        "ebitda": None,
        "fcf_yield": None,
        "ev_to_ebitda": None,
        "pe_ratio": None,
    }

    try:
        km = _fmp_get(session, api_key, "key-metrics-ttm", symbol=sym)
        if isinstance(km, list) and km:
            k0 = km[0]
            bundle["roic"] = _safe_float(k0.get("returnOnInvestedCapitalTTM"))
            bundle["roe"] = _safe_float(k0.get("returnOnEquityTTM"))
            bundle["fcf_yield"] = _safe_float(k0.get("freeCashFlowYieldTTM"))
            bundle["ev_to_ebitda"] = _safe_float(k0.get("evToEBITDATTM"))
            bundle["fcf"] = _safe_float(
                k0.get("freeCashFlowTTM")
                or k0.get("freeCashFlow")
                or k0.get("operatingCashFlowTTM")
            )
            bundle["market_cap"] = _safe_float(
                k0.get("marketCapTTM") or k0.get("marketCap") or k0.get("companyMarketCap")
            )
            bundle["ev"] = _safe_float(k0.get("enterpriseValueTTM") or k0.get("enterpriseValue"))
            bundle["ebitda"] = _safe_float(k0.get("ebitdaTTM") or k0.get("ebitda"))
            if bundle["debt_to_equity"] is None:
                bundle["debt_to_equity"] = _safe_float(k0.get("debtToEquityRatioTTM"))
    except Exception as e:
        print(f"  warning: key-metrics-ttm {sym}: {e}", file=sys.stderr)

    try:
        rt = _fmp_get(session, api_key, "ratios-ttm", symbol=sym)
        if isinstance(rt, list) and rt:
            r0 = rt[0]
            if bundle["debt_to_equity"] is None:
                bundle["debt_to_equity"] = _safe_float(r0.get("debtToEquityRatioTTM"))
            bundle["operating_margin"] = _safe_float(r0.get("operatingProfitMarginTTM"))
            bundle["gross_margin"] = _safe_float(r0.get("grossProfitMarginTTM"))
            bundle["pe_ratio"] = _safe_float(r0.get("priceToEarningsRatioTTM"))
            if bundle["ev_to_ebitda"] is None:
                bundle["ev_to_ebitda"] = _safe_float(r0.get("enterpriseValueMultipleTTM"))
    except Exception as e:
        print(f"  warning: ratios-ttm {sym}: {e}", file=sys.stderr)

    try:
        gr = _fmp_get(session, api_key, "income-statement-growth", symbol=sym, limit="8")
        if isinstance(gr, list) and gr:
            for row in gr:
                g = _safe_float(row.get("growthRevenue"))
                if g is not None:
                    bundle["revenue_growth"] = g
                    break
    except Exception as e:
        print(f"  warning: income-statement-growth {sym}: {e}", file=sys.stderr)

    # --- Derived metrics (do not overwrite good API values) ---
    mcap = bundle.get("market_cap")
    fcf = bundle.get("fcf")
    if bundle["fcf_yield"] is None and fcf is not None and mcap is not None and mcap > 0:
        bundle["fcf_yield"] = float(fcf) / float(mcap)

    ev_v, ebitda_v = bundle.get("ev"), bundle.get("ebitda")
    if bundle["ev_to_ebitda"] is None and ev_v is not None and ebitda_v is not None and abs(float(ebitda_v)) > 1e-9:
        bundle["ev_to_ebitda"] = float(ev_v) / float(ebitda_v)

    # PE must be positive for "cheap = high inverse score" logic downstream
    pe = bundle.get("pe_ratio")
    if pe is not None and pe <= 0:
        bundle["pe_ratio"] = None

    to_disk: dict[str, float | str | None] = {}
    for k, v in bundle.items():
        if k == "_schema":
            to_disk[k] = v
        elif v is None:
            to_disk[k] = None
        else:
            try:
                fv = float(v)
                to_disk[k] = None if math.isnan(fv) or math.isinf(fv) else fv
            except (TypeError, ValueError):
                to_disk[k] = None
    path.write_text(json.dumps(to_disk), encoding="utf-8")
    return {k: v for k, v in to_disk.items() if k != "_schema"}


def get_fundamentals_bundle(
    session: requests.Session,
    api_key: str,
    symbol: str,
    *,
    force_refresh: bool = False,
) -> dict[str, float | None]:
    """Backward-compatible subset for older scripts (maps to `get_fundamentals`)."""
    full = get_fundamentals(session, api_key, symbol, force_refresh=force_refresh)
    keys = (
        "roic",
        "roe",
        "fcf_yield",
        "revenue_growth",
        "debt_to_equity",
        "operating_margin",
        "gross_margin",
        "pe_ratio",
        "ev_to_ebitda",
    )
    return {k: full.get(k) for k in keys}


def _safe_float(x) -> float | None:
    try:
        if x is None or pd.isna(x):
            return None
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def pick_price_column(df: pd.DataFrame) -> str:
    """Prefer dividend-adjusted `adjClose`; fall back to `close` if needed."""
    if "adjClose" in df.columns and pd.to_numeric(df["adjClose"], errors="coerce").notna().any():
        return "adjClose"
    if "close" in df.columns:
        return "close"
    return "adjClose"


def _price_history_window_covers(df: pd.DataFrame, date_from: date, date_to: date) -> bool:
    if df.empty:
        return False
    ts = pd.to_datetime(df["date"], errors="coerce").dropna()
    if ts.empty:
        return False
    lo = ts.min().date()
    hi = ts.max().date()
    return lo <= date_from and hi >= date_to


def _filter_price_history_window(df: pd.DataFrame, date_from: date, date_to: date) -> pd.DataFrame:
    if df.empty:
        return df
    d0 = pd.Timestamp(date_from).normalize()
    d1 = pd.Timestamp(date_to).normalize()
    out = df.copy()
    out["_d"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    out = out[(out["_d"] >= d0) & (out["_d"] <= d1)].drop(columns=["_d"], errors="ignore")
    return out.reset_index(drop=True)


def _trim_price_history_cache(df: pd.DataFrame, as_of_date: date) -> pd.DataFrame:
    if df.empty:
        return df
    max_cal = int(getattr(config, "PRICE_HISTORY_CACHE_MAX_CALENDAR_DAYS", 900))
    cutoff = pd.Timestamp(as_of_date).normalize() - timedelta(days=max_cal)
    out = df.copy()
    out["_d"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    out = out[out["_d"].notna() & (out["_d"] >= cutoff)].drop(columns=["_d"], errors="ignore")
    return out.sort_values("date").reset_index(drop=True)


def _normalize_price_history_save_format(df: pd.DataFrame, sym: str) -> pd.DataFrame:
    """Standardize to sorted ``date``, ``adjClose``, ``symbol`` for disk + callers."""
    if df.empty:
        return pd.DataFrame(columns=["date", "adjClose", "symbol"])
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    col = pick_price_column(out)
    if col != "adjClose":
        out["adjClose"] = pd.to_numeric(out[col], errors="coerce")
    else:
        out["adjClose"] = pd.to_numeric(out["adjClose"], errors="coerce")
    out["symbol"] = sym
    return (
        out.dropna(subset=["date", "adjClose"])[["date", "adjClose", "symbol"]]
        .drop_duplicates(subset=["date"], keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )


def _fmp_fetch_adj_history_range(
    session: requests.Session,
    api_key: str,
    sym: str,
    date_from: date,
    date_to: date,
) -> pd.DataFrame:
    if date_from > date_to:
        return pd.DataFrame()
    meta_from = date_from.isoformat()
    meta_to = date_to.isoformat()
    try:
        raw = _fmp_get(
            session,
            api_key,
            "historical-price-eod/dividend-adjusted",
            **{"symbol": sym, "from": meta_from, "to": meta_to},
        )
    except Exception:
        return pd.DataFrame()
    if not isinstance(raw, list) or not raw:
        return pd.DataFrame()

    fetched = pd.DataFrame(raw)
    if "date" not in fetched.columns:
        return pd.DataFrame()

    fetched["date"] = pd.to_datetime(fetched["date"], errors="coerce").dt.normalize()
    col = pick_price_column(fetched)
    if col not in fetched.columns:
        return pd.DataFrame()

    fetched["adjClose"] = pd.to_numeric(fetched[col], errors="coerce")
    fetched["symbol"] = sym
    return (
        fetched.dropna(subset=["date", "adjClose"])[["date", "adjClose", "symbol"]]
        .drop_duplicates(subset=["date"], keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )


def _write_price_history_cache(path: Path, merged: pd.DataFrame, trim_as_of: date) -> None:
    trimmed = _trim_price_history_cache(merged, trim_as_of)
    if trimmed.empty:
        return
    pq_path = _parquet_cache_path(path.stem)
    try:
        trimmed.to_parquet(pq_path, index=False)
        return
    except Exception:
        trimmed.to_csv(path, index=False)


def get_price_history(
    session: requests.Session,
    api_key: str,
    symbol: str,
    date_from: date,
    date_to: date,
    *,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Daily dividend-adjusted prices for `symbol` between `date_from` and `date_to`.

    Cached locally under ``outputs/cache/prices`` (parquet preferred, CSV fallback). When possible,
    reused rows are merged with **incremental** FMP requests (prefix/suffix gaps) instead of
    reloading the entire window.
    With ``force_refresh=True``, the requested window is re-fetched and merged back into cache so
    newer corrected values replace stale rows while older history is retained.

    Caller receives rows filtered to ``[date_from, date_to]``. On-disk cache may hold more history
    (trimmed by ``PRICE_HISTORY_CACHE_MAX_CALENDAR_DAYS``).
    """
    sym = symbol.upper()
    path = _cache_path(sym)

    if force_refresh:
        full = _fmp_fetch_adj_history_range(session, api_key, sym, date_from, date_to)
        if full.empty:
            return pd.DataFrame()

        cached_force = _read_price_cache(sym)
        if not cached_force.empty:
            cached_force = _normalize_price_history_save_format(cached_force, sym)

        merged_force = (
            _normalize_price_history_save_format(pd.concat([cached_force, full], ignore_index=True), sym)
            if not cached_force.empty
            else full
        )
        _write_price_history_cache(path, merged_force, date_to)
        return _filter_price_history_window(merged_force, date_from, date_to)

    cached = _read_price_cache(sym)

    if not cached.empty:
        cached = _normalize_price_history_save_format(cached, sym)

    if cached.empty:
        full = _fmp_fetch_adj_history_range(session, api_key, sym, date_from, date_to)
        if not full.empty:
            _write_price_history_cache(path, full, date_to)
        return _filter_price_history_window(full, date_from, date_to)

    if _price_history_window_covers(cached, date_from, date_to):
        return _filter_price_history_window(cached, date_from, date_to)

    lo = cached["date"].min().date()
    hi = cached["date"].max().date()

    extras: list[pd.DataFrame] = []
    if lo > date_from:
        pref_to = lo - timedelta(days=1)
        if pref_to >= date_from:
            pre = _fmp_fetch_adj_history_range(session, api_key, sym, date_from, pref_to)
            if not pre.empty:
                extras.append(pre)

    if hi < date_to:
        suf_from = hi + timedelta(days=1)
        if suf_from <= date_to:
            suf = _fmp_fetch_adj_history_range(session, api_key, sym, suf_from, date_to)
            if not suf.empty:
                extras.append(suf)

    merged = (
        pd.concat([cached] + extras, ignore_index=True) if extras else cached.copy(deep=False)
    )
    merged = _normalize_price_history_save_format(merged, sym)

    if merged.empty:
        full = _fmp_fetch_adj_history_range(session, api_key, sym, date_from, date_to)
        if not full.empty:
            _write_price_history_cache(path, full, date_to)
        return _filter_price_history_window(full, date_from, date_to)

    if _price_history_window_covers(merged, date_from, date_to):
        _write_price_history_cache(path, merged, date_to)
        return _filter_price_history_window(merged, date_from, date_to)

    full_win = _fmp_fetch_adj_history_range(session, api_key, sym, date_from, date_to)
    merged_full = (
        _normalize_price_history_save_format(
            pd.concat([cached, full_win], ignore_index=True), sym,
        )
        if not full_win.empty
        else merged
    )

    if _price_history_window_covers(merged_full, date_from, date_to):
        _write_price_history_cache(path, merged_full, date_to)
        return _filter_price_history_window(merged_full, date_from, date_to)

    return _filter_price_history_window(merged_full, date_from, date_to)


def get_price_histories_long(
    session: requests.Session,
    api_key: str,
    symbols: Iterable[str],
    date_from: date,
    date_to: date,
    *,
    force_refresh: bool = False,
    max_workers: int | None = None,
) -> pd.DataFrame:
    """
    Dividend-adjusted closes for many symbols in long format: ``date``, ``symbol``, ``adjClose``.

    FMP exposes batch **quotes** and **single-day** EOD-bulk APIs, but not a multi-symbol
    dividend-adjusted **history** series in one call. This helper still issues one request
    per symbol when the CSV cache misses, but runs those requests concurrently (bounded
    pool) so cold loads and ``force_refresh`` complete faster.
    """
    seen: set[str] = set()
    syms: list[str] = []
    for s in symbols:
        u = str(s).upper().strip()
        if not u or u in seen:
            continue
        seen.add(u)
        syms.append(u)
    if not syms:
        return pd.DataFrame(columns=["date", "symbol", "adjClose"])

    def _normalize_hist(hist: pd.DataFrame, sym: str) -> pd.DataFrame:
        if hist.empty:
            return pd.DataFrame(columns=["date", "symbol", "adjClose"])
        col = pick_price_column(hist)
        if col not in hist.columns:
            return pd.DataFrame(columns=["date", "symbol", "adjClose"])
        h = hist[["date", col]].copy()
        h = h.rename(columns={col: "adjClose"})
        h["symbol"] = sym
        h["date"] = pd.to_datetime(h["date"], errors="coerce")
        h["adjClose"] = pd.to_numeric(h["adjClose"], errors="coerce")
        return h.dropna(subset=["date", "adjClose"])[["date", "symbol", "adjClose"]]

    chunks: list[pd.DataFrame] = []

    if len(syms) == 1:
        sym = syms[0]
        try:
            hist = get_price_history(session, api_key, sym, date_from, date_to, force_refresh=force_refresh)
        except Exception:
            return pd.DataFrame(columns=["date", "symbol", "adjClose"])
        one = _normalize_hist(hist, sym)
        if not one.empty:
            chunks.append(one)
    else:
        workers = max(1, min(int(max_workers or config.PRICE_FETCH_MAX_WORKERS), len(syms)))

        def _worker(sym: str) -> pd.DataFrame:
            try:
                local = create_http_session()
                hist = get_price_history(local, api_key, sym, date_from, date_to, force_refresh=force_refresh)
            except Exception:
                return pd.DataFrame(columns=["date", "symbol", "adjClose"])
            return _normalize_hist(hist, sym)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_worker, sym): sym for sym in syms}
            for fut in as_completed(futs):
                part = fut.result()
                if not part.empty:
                    chunks.append(part)

    if not chunks:
        return pd.DataFrame(columns=["date", "symbol", "adjClose"])
    return pd.concat(chunks, ignore_index=True).sort_values(["date", "symbol"])


def get_etf_prices(
    session: requests.Session,
    api_key: str,
    date_from: date,
    date_to: date,
    *,
    force_refresh: bool = False,
) -> dict[str, pd.DataFrame]:
    """
    Dividend-adjusted price history for each sector ETF in `config.SECTOR_ETF_MAP`.

    Returns: sector_name -> DataFrame(date, adjClose, ...)
    """
    items = list(config.SECTOR_ETF_MAP.items())
    unique_etfs = list(dict.fromkeys(str(etf).upper().strip() for _, etf in items))
    iterator = _progress(unique_etfs, desc="ETF prices")
    to_fetch = list(iterator)
    long_px = get_price_histories_long(
        session, api_key, to_fetch, date_from, date_to, force_refresh=force_refresh
    )
    out: dict[str, pd.DataFrame] = {}
    for sector, etf in items:
        eu = str(etf).upper().strip()
        if tqdm is None:
            print(f"[etf] {sector} ({etf})", flush=True)
        if long_px.empty or "symbol" not in long_px.columns:
            out[sector] = pd.DataFrame()
            print(f"  warning: no prices for {etf}", file=sys.stderr)
            continue
        sub = long_px.loc[long_px["symbol"] == eu].copy()
        if sub.empty:
            print(f"  warning: no prices for {etf}", file=sys.stderr)
            out[sector] = pd.DataFrame()
        else:
            out[sector] = sub.sort_values("date").reset_index(drop=True)
    _done_progress("ETF prices")
    return out


def prefetch_stock_prices(
    session: requests.Session,
    api_key: str,
    symbols: list[str],
    date_from: date,
    date_to: date,
    *,
    force_refresh: bool = False,
) -> None:
    """Bulk-download (with cache) stock prices; parallelizes uncached FMP pulls."""
    syms = [str(s).upper().strip() for s in symbols if str(s).strip()]
    if not syms:
        return
    iterator = _progress(syms, desc="Stock prices")
    syms_list = list(iterator)

    def _worker(sym: str) -> tuple[str, bool]:
        try:
            local = create_http_session()
            df = get_price_history(local, api_key, sym, date_from, date_to, force_refresh=force_refresh)
            return sym, not df.empty
        except Exception as e:
            print(f"  warning: {sym} failed: {e}", file=sys.stderr)
            return sym, False

    ok = 0
    if len(syms_list) == 1:
        _, good = _worker(syms_list[0])
        ok = int(good)
    else:
        workers = max(1, min(int(config.PRICE_FETCH_MAX_WORKERS), len(syms_list)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_worker, syms_list))
        ok = sum(1 for _, g in results if g)
        for i, sym in enumerate(syms_list, start=1):
            if i % 25 == 0 or i == 1 or i == len(syms_list):
                print(f"[prices] {i}/{len(syms_list)} {sym}", flush=True)
    _done_progress("Stock prices")
    print(f"[prices] Loaded non-empty histories for {ok}/{len(syms_list)} symbols.", flush=True)


def default_price_window(as_of: date | None = None) -> tuple[date, date]:
    """Default `from`/`to` for price pulls."""
    end = as_of or date.today()
    start = end - timedelta(days=config.PRICE_HISTORY_LOOKBACK_DAYS)
    return start, end
