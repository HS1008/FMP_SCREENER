"""
Build the full FMP `stock-list` ticker set, enrich with FMP `profile-bulk` (batched
`part=0,1,...` HTTP calls — not `response.json()` because bodies can be
concatenated JSON), left-merge into a **Full Dataset**, then apply the same
exchange / US / Technology rules as `get_stock_universe` (no min cap or volume)
to produce **Tech Universe** + **Industry Summary**.

Requires FMP access to `/stable/profile-bulk` (rate-limited; script backs off on
429 / "Limit Reach").

Run:
  python tech_universe.py
"""

from __future__ import annotations

import io
import json
import sys
import time
from datetime import datetime

import numpy as np
import pandas as pd
import requests
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

import config
import data_loader

TECH_SECTOR = "Technology"
OUTPUT_PATH = config.OUTPUT_DIR / "tech_universe.xlsx"
_EXCEL_MAX_STR = 32_000


def _sanitize_excel_cell(v):
    """Strip characters openpyxl rejects; keep numbers/timestamps as native types."""
    if v is None:
        return v
    if isinstance(v, bool) or isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        if pd.isna(v):
            return v
        return float(v)
    if isinstance(v, (pd.Timestamp, datetime)):
        return v
    s = str(v)
    s = ILLEGAL_CHARACTERS_RE.sub("", s)
    if len(s) > _EXCEL_MAX_STR:
        return s[: _EXCEL_MAX_STR - 3] + "..."
    return s


def sanitize_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in out.columns:
        out[c] = out[c].map(_sanitize_excel_cell)
    return out
# FMP profile-bulk is chunked; stop after this many empty parts in a row.
_PROFILE_BULK_MAX_EMPTY_STREAK = 3
_PROFILE_BULK_PART_SLEEP_S = 0.35
_PROFILE_BULK_429_SLEEP_S = 65.0
_PROFILE_BULK_MAX_PARTS = 500


def fetch_stock_list(session, api_key: str) -> pd.DataFrame:
    raw = data_loader._fmp_get(session, api_key, "stock-list")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("stock-list returned no rows.")
    df = pd.DataFrame(raw)
    if "symbol" not in df.columns:
        raise RuntimeError("stock-list missing symbol column.")
    df["symbol"] = df["symbol"].astype(str).str.upper().str.strip()
    if "companyName" not in df.columns:
        df["companyName"] = pd.NA
    return df[["symbol", "companyName"]].drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)


def _parse_profile_bulk_text(text: str) -> list[dict]:
    """
    FMP `profile-bulk` may return:
    - a single JSON array or object,
    - newline-delimited JSON objects,
    - or concatenated JSON objects (`{...}{...}`) which breaks `response.json()`.
    """
    text = (text or "").strip()
    if not text:
        return []

    def _error_dict(d: dict) -> None:
        if d.get("Error Message"):
            raise RuntimeError(str(d["Error Message"]))

    try:
        val = json.loads(text)
        if isinstance(val, dict):
            _error_dict(val)
            return [val]
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    _error_dict(item)
            return [x for x in val if isinstance(x, dict)]
    except json.JSONDecodeError:
        pass

    # FMP commonly returns CSV (quoted header row) for profile-bulk downloads.
    try:
        df_csv = pd.read_csv(io.StringIO(text), low_memory=False)
        if len(df_csv.columns) and "symbol" in [c.lower() for c in df_csv.columns.astype(str)]:
            return df_csv.to_dict("records")
    except (ValueError, pd.errors.EmptyDataError, pd.errors.ParserError):
        pass

    try:
        df = pd.read_json(io.StringIO(text), lines=True)
        if len(df.columns) > 0:
            recs = df.to_dict("records")
            for rec in recs:
                if isinstance(rec, dict) and rec.get("Error Message"):
                    raise RuntimeError(str(rec["Error Message"]))
            return [r for r in recs if isinstance(r, dict)]
    except ValueError:
        pass

    dec = json.JSONDecoder()
    idx = 0
    rows: list[dict] = []
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        try:
            val, end = dec.raw_decode(text, idx)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                "profile-bulk: could not parse JSON body. "
                f"First bytes: {repr(text[:160])}"
            ) from e
        if isinstance(val, dict):
            _error_dict(val)
            rows.append(val)
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    _error_dict(item)
                    rows.append(item)
        idx = end
    return rows


def _profile_bulk_http_get(session: requests.Session, api_key: str, part: int) -> list[dict]:
    url = f"{config.FMP_BASE_URL.rstrip('/')}/profile-bulk"
    r = session.get(url, params={"apikey": api_key, "part": str(part)}, timeout=config.HTTP_TIMEOUT_S)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig")
    return _parse_profile_bulk_text(text)


def _fetch_profile_bulk_part(session, api_key: str, part: int) -> list[dict]:
    """GET profile-bulk with 429 / FMP limit backoff (bulk endpoint is rate-limited)."""
    attempts = 0
    while True:
        attempts += 1
        try:
            return _profile_bulk_http_get(session, api_key, part)
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else None
            if code == 429 and attempts <= 6:
                print(
                    f"[tech_universe] profile-bulk part={part}: HTTP {code}, "
                    f"sleep {_PROFILE_BULK_429_SLEEP_S:.0f}s (attempt {attempts})...",
                    flush=True,
                )
                time.sleep(_PROFILE_BULK_429_SLEEP_S)
                continue
            raise
        except RuntimeError as e:
            err = str(e).lower()
            if ("limit" in err or "429" in err) and attempts <= 6:
                print(
                    f"[tech_universe] profile-bulk part={part}: {e!s} — "
                    f"sleep {_PROFILE_BULK_429_SLEEP_S:.0f}s (attempt {attempts})...",
                    flush=True,
                )
                time.sleep(_PROFILE_BULK_429_SLEEP_S)
                continue
            raise


def fetch_profile_bulk_all(
    session,
    api_key: str,
    *,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Concatenate all `profile-bulk` parts (FMP batch company profiles).

    Results are pickled to ``config.PROFILE_BULK_CACHE_PATH`` (TTL ``PROFILE_BULK_CACHE_TTL_SECONDS``)
    so dashboard dispersion does not re-download the full bulk chain on every cold cache refresh.
    """
    cache_path = getattr(config, "PROFILE_BULK_CACHE_PATH", None)
    ttl = float(getattr(config, "PROFILE_BULK_CACHE_TTL_SECONDS", 6 * 3600))
    if (
        cache_path is not None
        and not force_refresh
        and cache_path.is_file()
        and (time.time() - cache_path.stat().st_mtime) < ttl
    ):
        try:
            return pd.read_pickle(cache_path)
        except Exception:
            pass

    chunks: list[pd.DataFrame] = []
    part = 0
    empty_streak = 0
    while part < _PROFILE_BULK_MAX_PARTS:
        print(f"[tech_universe] profile-bulk part={part} ...", flush=True)
        try:
            rows = _fetch_profile_bulk_part(session, api_key, part)
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code in (400, 404):
                print(
                    f"[tech_universe] profile-bulk: no more parts after part={part} (HTTP {code}).",
                    flush=True,
                )
                break
            raise
        if not rows:
            empty_streak += 1
            if empty_streak >= _PROFILE_BULK_MAX_EMPTY_STREAK:
                break
        else:
            empty_streak = 0
            chunks.append(pd.DataFrame(rows))
        part += 1
        time.sleep(_PROFILE_BULK_PART_SLEEP_S)

    if not chunks:
        return pd.DataFrame()

    df = pd.concat(chunks, ignore_index=True)
    if "symbol" not in df.columns:
        raise RuntimeError("profile-bulk data missing symbol.")
    df["symbol"] = df["symbol"].astype(str).str.upper().str.strip()
    df = df.drop_duplicates(subset=["symbol"], keep="last")
    out = df.reset_index(drop=True)

    if cache_path is not None and not out.empty:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
            out.to_pickle(tmp)
            tmp.replace(cache_path)
        except Exception as e:
            print(f"[tech_universe] profile-bulk cache write failed: {e}", file=sys.stderr)

    return out


def merge_stock_list_with_profiles(stock_list: pd.DataFrame, profiles: pd.DataFrame) -> pd.DataFrame:
    """Left-join: every stock-list ticker, plus profile columns when FMP provides them."""
    if profiles.empty:
        return stock_list.copy()
    prof = profiles.copy()
    drop_name = "companyName" if "companyName" in prof.columns else None
    if drop_name:
        prof = prof.drop(columns=[drop_name])
    out = stock_list.merge(prof, on="symbol", how="left")
    return out


def _normalize_exchange_column(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "exchangeShortName" not in out.columns and "exchange" in out.columns:
        out["exchangeShortName"] = out["exchange"]
    return out


def _truthy_mask(s: pd.Series) -> pd.Series:
    """FMP CSV booleans are often the strings 'True' / 'False'."""
    return s.astype(str).str.lower().str.strip().isin(("true", "1", "t", "yes"))


def apply_tech_universe_filters(df: pd.DataFrame) -> pd.DataFrame:
    """
    US Technology equities: major exchanges, actively trading, not ETF/fund,
    symbol heuristics. No minimum market cap or volume.
    Expects merged profile-style columns (see FMP /stable/profile).
    """
    if df.empty:
        return df
    out = df.copy()
    out = _normalize_exchange_column(out)

    if "isActivelyTrading" in out.columns:
        out = out[_truthy_mask(out["isActivelyTrading"])]

    out["marketCap"] = pd.to_numeric(out.get("marketCap"), errors="coerce")
    vol_parts: list[pd.Series] = []
    if "averageVolume" in out.columns:
        vol_parts.append(pd.to_numeric(out["averageVolume"], errors="coerce"))
    if "volume" in out.columns:
        vol_parts.append(pd.to_numeric(out["volume"], errors="coerce"))
    if vol_parts:
        out["volume"] = vol_parts[0]
        for s in vol_parts[1:]:
            out["volume"] = out["volume"].combine_first(s)
    else:
        out["volume"] = np.nan

    if "isEtf" in out.columns:
        out = out[~_truthy_mask(out["isEtf"])]
    if "isFund" in out.columns:
        out = out[~_truthy_mask(out["isFund"])]

    if "exchangeShortName" in out.columns:
        out = out[out["exchangeShortName"].isin(config.US_MAJOR_EXCHANGES)]

    if "country" in out.columns:
        out = out[out["country"].astype(str).str.upper() == "US"]

    if "sector" in out.columns:
        out = out[out["sector"].astype(str).str.strip() == TECH_SECTOR]

    if "symbol" in out.columns:
        out = out[~out["symbol"].astype(str).map(data_loader._looks_like_warrant_or_right)]
    if "industry" in out.columns:
        out = out[~out["industry"].map(data_loader._looks_like_warrant_industry)]

    if "symbol" in out.columns:
        out = out[~out["symbol"].astype(str).map(data_loader._looks_like_preferred_or_unit)]

    out = out.dropna(subset=["symbol", "industry"])
    out = out.drop_duplicates(subset=["symbol"], keep="first")
    return out.reset_index(drop=True)


def build_industry_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(
            columns=[
                "industry",
                "company_count",
                "median_market_cap",
                "total_market_cap",
                "median_volume",
                "largest_company",
                "largest_symbol",
            ]
        )

    g = df.groupby("industry", dropna=False)
    rows_out: list[dict] = []
    for industry, part in g:
        part = part.sort_values("marketCap", ascending=False)
        top = part.iloc[0]
        rows_out.append(
            {
                "industry": industry,
                "company_count": int(len(part)),
                "median_market_cap": float(part["marketCap"].median()),
                "total_market_cap": float(part["marketCap"].sum()),
                "median_volume": float(part["volume"].median()),
                "largest_company": str(top.get("companyName", "") or ""),
                "largest_symbol": str(top.get("symbol", "") or ""),
            }
        )
    summary = pd.DataFrame(rows_out)
    summary = summary.sort_values("company_count", ascending=False).reset_index(drop=True)
    return summary


def main() -> int:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    api_key = data_loader.load_api_key()
    session = data_loader.create_http_session()

    print("[tech_universe] Downloading full stock-list ...", flush=True)
    stock_list = fetch_stock_list(session, api_key)
    print(f"[tech_universe] stock-list tickers: {len(stock_list):,}", flush=True)

    print("[tech_universe] Downloading profile-bulk (all parts) ...", flush=True)
    profiles = fetch_profile_bulk_all(session, api_key)
    if profiles.empty:
        print(
            "[tech_universe] ERROR: profile-bulk returned no data (rate limit or plan). "
            "Retry later or check FMP bulk endpoint access.",
            file=sys.stderr,
        )
        return 1
    print(f"[tech_universe] profile-bulk unique symbols: {len(profiles):,}", flush=True)

    full_dataset = merge_stock_list_with_profiles(stock_list, profiles)
    matched = full_dataset["sector"].notna().sum() if "sector" in full_dataset.columns else 0
    print(f"[tech_universe] merged rows with profile sector present: {matched:,}", flush=True)

    tech_raw = apply_tech_universe_filters(full_dataset)

    cols = ["symbol", "companyName", "sector", "industry", "marketCap", "volume", "exchangeShortName"]
    for c in cols:
        if c not in tech_raw.columns:
            tech_raw[c] = pd.NA
    tech_out = tech_raw[cols].copy()
    tech_out = tech_out.sort_values(["industry", "marketCap"], ascending=[True, False]).reset_index(drop=True)

    work = tech_out.copy()
    work["marketCap"] = pd.to_numeric(work["marketCap"], errors="coerce")
    work["volume"] = pd.to_numeric(work["volume"], errors="coerce")
    summary = build_industry_summary(work.dropna(subset=["industry", "marketCap"]))

    print(f"\nTotal Technology companies passing filters: {len(tech_out)}", flush=True)
    print("\nIndustry counts (largest to smallest):", flush=True)
    if summary.empty:
        print("(no rows)", flush=True)
    else:
        print(
            summary[["industry", "company_count"]].to_string(index=False),
            flush=True,
        )

    with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
        sanitize_for_excel(stock_list).to_excel(writer, sheet_name="All Stock Tickers", index=False)
        sanitize_for_excel(full_dataset).to_excel(writer, sheet_name="Full Dataset", index=False)
        sanitize_for_excel(tech_out).to_excel(writer, sheet_name="Tech Universe", index=False)
        sanitize_for_excel(summary).to_excel(writer, sheet_name="Industry Summary", index=False)

    print(f"\nWrote: {OUTPUT_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
