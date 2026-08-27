"""Parse QuantConnect parameterSet, statistics, and equity chart payloads.

Canonical numeric storage:
  returns / drawdowns / win rates / PSR = decimal (0.12 means 12%)
  Sharpe / Sortino / Alpha / Beta = ratio / raw value
  trade_count = int

Missing or malformed values become None — never 0 for ranking.
"""

from __future__ import annotations

import json
import math
from typing import Any


RESEARCH_PREFIX = "research_"

STAT_ALIASES = {
    "sharpe_ratio": ("sharpe_ratio", "Sharpe Ratio", "SharpeRatio", "sharpeRatio", "Sharpe"),
    "sortino_ratio": ("sortino_ratio", "Sortino Ratio", "SortinoRatio", "sortinoRatio", "Sortino"),
    "cagr": (
        "cagr",
        "Compounding Annual Return",
        "compoundingAnnualReturn",
        "CompoundingAnnualReturn",
        "CAGR",
    ),
    "max_drawdown": (
        "max_drawdown",
        "Drawdown",
        "drawdown",
        "Max Drawdown",
        "maxDrawdown",
        "Net Drawdown",
    ),
    "net_profit": (
        "net_profit",
        "Net Profit",
        "netProfit",
        "Total Net Profit",
        "totalNetProfit",
    ),
    "alpha": ("alpha", "Alpha"),
    "beta": ("beta", "Beta"),
    "win_rate": ("win_rate", "Win Rate", "winRate", "WinRate"),
    "loss_rate": ("loss_rate", "Loss Rate", "lossRate", "LossRate"),
    "trade_count": (
        "trade_count",
        "Total Orders",
        "Total Trades",
        "trades",
        "Trades",
        "totalOrders",
        "totalTrades",
    ),
    "psr": ("psr", "Probabilistic Sharpe Ratio", "probabilisticSharpeRatio", "PSR"),
}

PERCENT_KEYS = {"cagr", "max_drawdown", "net_profit", "win_rate", "loss_rate", "psr"}
RATIO_KEYS = {"sharpe_ratio", "sortino_ratio", "alpha", "beta"}

RESEARCH_FIELD_MAP = {
    "research_suite_version": "research_suite_version",
    "research_run_id": "research_run_id",
    "research_experiment_id": "research_experiment_id",
    "research_test_type": "research_test_type",
    "research_phase": "research_phase",
    "research_window_id": "research_window_id",
    "research_git_commit": "research_git_commit",
    "research_is_holdout": "research_is_holdout",
    "research_train_start": "train_start",
    "research_train_end": "train_end",
    "research_test_start": "test_start",
    "research_test_end": "test_end",
    "research_objective": "objective_name",
    "research_thresholds": "research_thresholds_raw",
    "research_primary_parameter": "research_primary_parameter",
    "research_dirty": "research_dirty",
    "research_strategy_id": "research_strategy_id",
}

STRATEGY_PARAM_KEYS_HINT = {
    "start_date",
    "end_date",
    "sma_period",
    "starting_cash",
}


def _missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() in {"", "-", "NaN", "nan", "None", "null"}:
        return True
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return True
    return False


def parse_number(value: Any) -> float | None:
    if _missing(value):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        return float(text)
    except ValueError:
        return None


def parse_percent_to_decimal(value: Any) -> float | None:
    """Store percents as decimals. '12%' -> 0.12. Bare numbers > 1 treated as percent-points."""
    if _missing(value):
        return None
    had_percent = isinstance(value, str) and "%" in value
    number = parse_number(value)
    if number is None:
        return None
    if had_percent or abs(number) > 1.0:
        return number / 100.0
    return number


def _lookup(source: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    lowered = {str(key).strip().lower(): key for key in source}
    for alias in aliases:
        if alias in source and not _missing(source[alias]):
            return source[alias]
        key = lowered.get(alias.lower())
        if key is not None and not _missing(source[key]):
            return source[key]
    return None


def _statistic_maps(raw: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    maps: list[dict[str, Any]] = []
    for key in ("statistics", "Statistics", "runtimeStatistics", "totalPerformance"):
        value = raw.get(key)
        if isinstance(value, dict):
            maps.append(value)
            nested = value.get("statistics") or value.get("Statistics")
            if isinstance(nested, dict):
                maps.append(nested)
    maps.append(raw)
    backtest = raw.get("backtest")
    if isinstance(backtest, dict):
        maps.extend(_statistic_maps(backtest))
    return maps


def extract_statistic(raw: dict[str, Any] | None, canonical: str) -> Any:
    for mapping in _statistic_maps(raw):
        value = _lookup(mapping, STAT_ALIASES[canonical])
        if not _missing(value):
            return value
    return None


def normalize_statistics(raw: dict[str, Any] | None, *, failed: bool = False) -> dict[str, Any]:
    result = {
        "sharpe_ratio": None,
        "sortino_ratio": None,
        "cagr": None,
        "max_drawdown": None,
        "net_profit": None,
        "alpha": None,
        "beta": None,
        "win_rate": None,
        "loss_rate": None,
        "trade_count": None,
        "psr": None,
        "failed": bool(failed),
    }
    if failed or not raw:
        return result
    for key in RATIO_KEYS:
        result[key] = parse_number(extract_statistic(raw, key))
    for key in PERCENT_KEYS:
        result[key] = parse_percent_to_decimal(extract_statistic(raw, key))
    trades = parse_number(extract_statistic(raw, "trade_count"))
    result["trade_count"] = int(trades) if trades is not None else None
    return result


def extract_parameter_set(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for key in ("parameterSet", "ParameterSet", "parameters"):
        value = payload.get(key)
        if isinstance(value, dict):
            return dict(value)
    backtest = payload.get("backtest")
    if isinstance(backtest, dict):
        return extract_parameter_set(backtest)
    return {}


def split_parameters(parameter_set: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (research_metadata, strategy_parameters)."""
    research: dict[str, Any] = {}
    strategy: dict[str, Any] = {}
    for key, value in (parameter_set or {}).items():
        name = str(key)
        if name.startswith(RESEARCH_PREFIX):
            research[name] = value
        else:
            strategy[name] = value
    return research, strategy


def _as_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _as_json(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return None


def parse_name_fallback(name: str | None) -> dict[str, Any]:
    """Fallback parser for S1__strategy__run__type__window__seq names."""
    parsed = {
        "research_suite_version": None,
        "research_run_id": None,
        "research_test_type": None,
        "research_window_id": None,
        "research_strategy_id": None,
    }
    if not name or not str(name).startswith("S1__"):
        return parsed
    parts = str(name).split("__")
    if len(parts) < 6:
        return parsed
    parsed["research_suite_version"] = parts[0]
    parsed["research_strategy_id"] = parts[1]
    parsed["research_run_id"] = parts[2]
    parsed["research_test_type"] = parts[3]
    parsed["research_window_id"] = parts[4]
    return parsed


def extract_stage1_metadata(
    payload: dict[str, Any] | None,
    *,
    name: str | None = None,
) -> dict[str, Any]:
    """Primary source is parameterSet. Name parsing is fallback only."""
    parameter_set = extract_parameter_set(payload)
    research, strategy = split_parameters(parameter_set)
    fallback = parse_name_fallback(name or (payload or {}).get("name"))

    meta = {
        "research_suite_version": None,
        "research_run_id": None,
        "research_experiment_id": None,
        "research_test_type": None,
        "research_phase": None,
        "research_window_id": None,
        "research_git_commit": None,
        "research_is_holdout": None,
        "research_dirty": None,
        "research_strategy_id": None,
        "research_primary_parameter": None,
        "objective_name": None,
        "train_start": None,
        "train_end": None,
        "test_start": None,
        "test_end": None,
        "thresholds": None,
        "parameters": strategy,
        "research_parameters": research,
        "source": "parameterSet" if research else ("name" if fallback["research_run_id"] else None),
    }

    for source_key, dest_key in RESEARCH_FIELD_MAP.items():
        if source_key in research and research[source_key] not in (None, ""):
            meta[dest_key] = research[source_key]

    meta["research_is_holdout"] = _as_bool(meta.get("research_is_holdout"))
    meta["research_dirty"] = _as_bool(meta.get("research_dirty"))
    meta["thresholds"] = _as_json(research.get("research_thresholds") or meta.get("research_thresholds_raw"))
    meta.pop("research_thresholds_raw", None)

    if not meta["research_run_id"]:
        meta["research_run_id"] = fallback["research_run_id"]
        if fallback["research_run_id"]:
            meta["source"] = "name"
    if not meta["research_test_type"]:
        meta["research_test_type"] = fallback["research_test_type"]
    if not meta["research_window_id"]:
        meta["research_window_id"] = fallback["research_window_id"]
    if not meta["research_suite_version"] and fallback["research_suite_version"]:
        meta["research_suite_version"] = fallback["research_suite_version"]
    if not meta["research_strategy_id"]:
        meta["research_strategy_id"] = fallback["research_strategy_id"]

    start = strategy.get("start_date")
    end = strategy.get("end_date")
    if not meta["test_start"]:
        meta["test_start"] = start
    if not meta["test_end"]:
        meta["test_end"] = end

    if meta["research_is_holdout"] is None and meta["research_test_type"]:
        meta["research_is_holdout"] = str(meta["research_test_type"]).endswith("HOLDOUT") or str(
            meta["research_test_type"]
        ) == "FINAL_HOLDOUT"

    return meta


def is_stage1_name(name: str | None) -> bool:
    return bool(name) and str(name).startswith("S1__")


def is_failed_status(status: str | None, payload: dict[str, Any] | None = None) -> bool:
    text = str(status or "").lower()
    if payload:
        error = payload.get("error") or payload.get("stacktrace")
        if error:
            return True
    return any(token in text for token in ("runtime error", "runtimeerror", "invalid", "error"))


def downsample_points(points: list[tuple[Any, float]], max_points: int = 1000) -> list[tuple[Any, float]]:
    if max_points < 2 or len(points) <= max_points:
        return points
    if len(points) <= 2:
        return points
    step = (len(points) - 1) / float(max_points - 1)
    chosen = []
    used = set()
    for i in range(max_points):
        index = int(round(i * step))
        if index in used:
            continue
        used.add(index)
        chosen.append(points[index])
    if points[-1] not in chosen:
        chosen.append(points[-1])
    return chosen[:max_points]


def _coerce_timestamp(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e12:
            number = number / 1000.0
        return number
    return value


def _series_points(series: Any) -> list[tuple[Any, float]]:
    if series is None:
        return []
    values = series
    if isinstance(series, dict):
        for key in ("values", "Values", "y"):
            if key in series:
                values = series[key]
                break
    if not isinstance(values, list):
        return []
    points = []
    for item in values:
        timestamp = None
        equity = None
        if isinstance(item, (list, tuple)):
            if len(item) >= 2:
                timestamp = item[0]
                equity = item[-1] if len(item) >= 5 else item[1]
        elif isinstance(item, dict):
            timestamp = item.get("x") or item.get("time") or item.get("timestamp")
            equity = item.get("y") or item.get("value") or item.get("close")
        number = parse_number(equity)
        ts = _coerce_timestamp(timestamp)
        if ts is None or number is None:
            continue
        points.append((ts, number))
    return points


def parse_equity_chart(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Extract Strategy Equity points. Prefer a series named Equity."""
    if not isinstance(payload, dict):
        return []
    if str(payload.get("status") or "").lower() == "loading":
        return []
    chart = payload.get("chart") or payload.get("Chart") or payload
    series_map = None
    if isinstance(chart, dict):
        series_map = chart.get("series") or chart.get("Series") or chart.get("charts")
        if series_map is None and "values" in chart:
            series_map = {"Equity": chart}
    if isinstance(payload.get("series"), dict):
        series_map = payload.get("series")
    if not isinstance(series_map, dict) or not series_map:
        return []

    preferred_names = ("Equity", "equity", "Strategy Equity", "Equity:")
    series_name = None
    series = None
    for name in preferred_names:
        if name in series_map:
            series_name = name
            series = series_map[name]
            break
    if series is None:
        series_name, series = next(iter(series_map.items()))

    points = _series_points(series)
    if not points:
        for name, candidate in series_map.items():
            points = _series_points(candidate)
            if points:
                series_name = name
                break
    points = downsample_points(points, 1000)
    rows = []
    previous = None
    for timestamp, equity in points:
        period_return = None
        if previous not in (None, 0):
            period_return = (equity / previous) - 1.0
        rows.append(
            {
                "timestamp": timestamp,
                "equity": equity,
                "period_return": period_return,
                "series_name": series_name or "Equity",
            }
        )
        previous = equity
    return rows
