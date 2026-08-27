"""Stage 1 grouping, WFO aggregates, comparison backtest selection, assessment."""

from __future__ import annotations

from typing import Any

import pandas as pd


PASS = "PASS"
WATCH = "WATCH"
FAIL = "FAIL"

DEFAULT_THRESHOLDS = {
    "min_validation_sharpe": 0.0,
    "min_oos_is_sharpe_ratio": 0.60,
    "min_profitable_wfo_fraction": 0.70,
    "min_median_wfo_sharpe": 0.0,
    "min_positive_parameter_fraction": 0.60,
}

COMPARISON_PRIORITY = ("FINAL_HOLDOUT", "VALIDATION", "BASELINE_DEV")


def is_stage1(df: pd.DataFrame) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype=bool)
    if "research_suite_version" in df.columns:
        version = df["research_suite_version"].fillna("").astype(str)
        return version.str.upper().isin({"S1", "S1.0"}) | version.str.startswith("S1")
    return pd.Series([False] * len(df), index=df.index)


def legacy_backtests(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    mask = ~is_stage1(df)
    if "research_run_id" in df.columns:
        mask = mask & df["research_run_id"].isna()
    return df.loc[mask].copy()


def stage1_backtests(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    return df.loc[is_stage1(df)].copy()


def research_runs(df: pd.DataFrame) -> pd.DataFrame:
    stage = stage1_backtests(df)
    if stage is None or stage.empty:
        return pd.DataFrame()
    work = stage.copy()
    if "strategy_id" not in work.columns:
        work["strategy_id"] = None
    if "research_git_commit" not in work.columns:
        work["research_git_commit"] = None
    if "research_is_holdout" not in work.columns:
        work["research_is_holdout"] = False
    if "created_at" not in work.columns:
        work["created_at"] = pd.NaT
    grouped = (
        work.groupby("research_run_id", dropna=False)
        .agg(
            strategy_id=("strategy_id", "first"),
            git_commit=("research_git_commit", "first"),
            suite_version=("research_suite_version", "first"),
            n_backtests=("backtest_id", "count"),
            holdout_accessed=("research_is_holdout", lambda s: bool(pd.Series(s).fillna(False).astype(bool).any())),
            first_created=("created_at", "min"),
            last_created=("created_at", "max"),
        )
        .reset_index()
    )
    grouped = grouped.sort_values("last_created", ascending=False)
    return grouped


def holdout_access_count(df: pd.DataFrame, git_commit: str | None = None) -> int:
    stage = stage1_backtests(df)
    if stage is None or stage.empty:
        return 0
    holdout = stage.copy()
    if "research_is_holdout" in holdout.columns:
        holdout = holdout[holdout["research_is_holdout"].fillna(False).astype(bool)]
    if git_commit:
        holdout = holdout[holdout["research_git_commit"].astype(str).str.replace("-dirty", "", regex=False) == str(git_commit).replace("-dirty", "")]
    if holdout.empty:
        return 0
    if "research_run_id" in holdout.columns:
        return int(holdout["research_run_id"].nunique())
    return int(len(holdout))


def select_comparison_backtest(df: pd.DataFrame) -> dict[str, Any] | None:
    """Prefer FINAL_HOLDOUT, then VALIDATION, then BASELINE_DEV, else latest legacy.

    Comparison is taken from the latest Stage 1 research run so a random
    parameter-grid or WFO training backtest is never used.
    """
    if df is None or df.empty:
        return None
    stage = stage1_backtests(df)
    if stage is not None and not stage.empty and "research_test_type" in stage.columns:
        if "research_run_id" in stage.columns:
            runs = research_runs(stage)
            if runs is not None and not runs.empty:
                latest_run = runs.iloc[0]["research_run_id"]
                stage = stage[stage["research_run_id"] == latest_run]
        for test_type in COMPARISON_PRIORITY:
            matches = stage[stage["research_test_type"] == test_type]
            if not matches.empty:
                row = matches.iloc[0]
                return {
                    "row": row,
                    "source": "stage1",
                    "test_type": test_type,
                    "label": _comparison_label(row, test_type),
                }
    # Latest backtest fallback (legacy / non-stage1)
    ordered = df
    if "created_at" in df.columns:
        ordered = df.sort_values("created_at", ascending=False)
    row = ordered.iloc[0]
    return {
        "row": row,
        "source": "legacy",
        "test_type": None,
        "label": "Latest synced backtest (legacy fallback) — {0}".format(row.get("name") or row.get("backtest_id")),
    }


def _comparison_label(row: Any, test_type: str) -> str:
    names = {
        "FINAL_HOLDOUT": "Stage 1 Final Holdout",
        "VALIDATION": "Stage 1 Validation",
        "BASELINE_DEV": "Stage 1 Development Baseline",
    }
    title = names.get(test_type, test_type)
    start = row.get("test_start") or row.get("backtest_start")
    end = row.get("test_end") or row.get("backtest_end")
    params = row.get("parameters_json") or {}
    param_text = ""
    if isinstance(params, dict):
        sma = params.get("sma_period")
        if sma is not None:
            param_text = " — SMA {0}".format(sma)
    window = ""
    if start and end:
        window = " — {0}-{1}".format(str(start)[:10], str(end)[:10])
    return "{0}{1}{2}".format(title, window, param_text)


def filter_test_type(df: pd.DataFrame, test_type: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    return df[df["research_test_type"] == test_type].copy()


def walk_forward_aggregates(df: pd.DataFrame, *, include_holdout: bool = False) -> dict[str, Any]:
    empty = {
        "n_windows": 0,
        "n_profitable": 0,
        "profitable_fraction": None,
        "median_oos_sharpe": None,
        "mean_oos_sharpe": None,
        "worst_oos_sharpe": None,
        "median_oos_cagr": None,
        "worst_max_drawdown": None,
        "selected_parameter_history": [],
        "unique_selected_parameters": [],
        "parameter_selection_stability": None,
    }
    if df is None or df.empty:
        return empty
    tests = df[df["research_test_type"].isin(["WFO_TEST", "WFO_TEST_HOLDOUT"])].copy()
    if not include_holdout:
        if "research_is_holdout" in tests.columns:
            tests = tests[~tests["research_is_holdout"].fillna(False).astype(bool)]
        tests = tests[tests["research_test_type"] == "WFO_TEST"]
    if tests.empty:
        return empty

    sharpes = pd.to_numeric(tests["sharpe_ratio"], errors="coerce").dropna()
    cagrs = pd.to_numeric(tests["cagr"], errors="coerce").dropna() if "cagr" in tests.columns else pd.Series(dtype=float)
    dds = pd.to_numeric(tests["max_drawdown"], errors="coerce").dropna() if "max_drawdown" in tests.columns else pd.Series(dtype=float)
    nets = pd.to_numeric(tests["net_profit"], errors="coerce") if "net_profit" in tests.columns else pd.Series([None] * len(tests))
    profitable = int((nets.fillna(0) > 0).sum()) if nets.notna().any() else int((sharpes > 0).sum())
    n = int(len(tests))
    history = []
    for _, row in tests.iterrows():
        params = row.get("parameters_json") or {}
        if isinstance(params, dict):
            primary = row.get("research_primary_parameter") or "sma_period"
            history.append(params.get(primary))
        else:
            history.append(None)
    unique = sorted({str(v) for v in history if v is not None})
    stability = None
    if history:
        stability = 1.0 - ((len(unique) - 1) / float(max(len(history), 1)))
    return {
        "n_windows": n,
        "n_profitable": profitable,
        "profitable_fraction": profitable / float(n) if n else None,
        "median_oos_sharpe": float(sharpes.median()) if not sharpes.empty else None,
        "mean_oos_sharpe": float(sharpes.mean()) if not sharpes.empty else None,
        "worst_oos_sharpe": float(sharpes.min()) if not sharpes.empty else None,
        "median_oos_cagr": float(cagrs.median()) if not cagrs.empty else None,
        "worst_max_drawdown": float(dds.min()) if not dds.empty else None,
        "selected_parameter_history": history,
        "unique_selected_parameters": unique,
        "parameter_selection_stability": stability,
    }


def _first_row(df: pd.DataFrame, test_type: str) -> pd.Series | None:
    matches = filter_test_type(df, test_type)
    if matches is None or matches.empty:
        return None
    return matches.iloc[0]


def parameter_robustness_summary(param_sens: pd.DataFrame, *, primary: str = "sma_period", default_value: Any = 200) -> dict[str, Any]:
    if param_sens is None or param_sens.empty:
        return {
            "raw_best_parameter": None,
            "selected_parameter": None,
            "best_sharpe": None,
            "selected_sharpe": None,
            "positive_parameter_fraction": None,
            "plateau_width": None,
            "robustness_label": "insufficient_data",
        }
    rows = []
    for _, row in param_sens.iterrows():
        params = row.get("parameters_json") or {}
        if not isinstance(params, dict):
            continue
        try:
            param = float(params.get(primary))
        except (TypeError, ValueError):
            continue
        sharpe = pd.to_numeric(pd.Series([row.get("sharpe_ratio")]), errors="coerce").iloc[0]
        if pd.isna(sharpe):
            continue
        rows.append((param, float(sharpe)))
    if not rows:
        return {
            "raw_best_parameter": None,
            "selected_parameter": None,
            "best_sharpe": None,
            "selected_sharpe": None,
            "positive_parameter_fraction": None,
            "plateau_width": None,
            "robustness_label": "insufficient_data",
        }
    rows.sort(key=lambda item: item[0])
    sharpes = [item[1] for item in rows]
    best = max(sharpes)
    raw_best = min((item for item in rows if item[1] == best), key=lambda item: abs(item[0] - float(default_value or 0)))[0]
    positive = sum(1 for value in sharpes if value > 0) / float(len(sharpes))
    threshold = 0.90 * best if best > 0 else best
    plateau = [item for item in rows if item[1] >= threshold]
    # Neighbor-mean selection among plateau members
    param_list = [item[0] for item in rows]

    def neighbor_mean(param: float) -> float:
        index = param_list.index(param)
        values = []
        for offset in range(index - 1, index + 2):
            if 0 <= offset < len(sharpes):
                values.append(sharpes[offset])
        return sum(values) / float(len(values)) if values else 0.0

    solid = [item for item in (plateau or rows) if neighbor_mean(item[0]) >= threshold]
    if not solid:
        solid = list(plateau or rows)
    selected = min(
        solid,
        key=lambda item: (
            abs(item[0] - float(default_value or 0)),
            -neighbor_mean(item[0]),
        ),
    )
    label = "isolated_peak" if len(plateau) <= 1 else ("stable_plateau" if len(plateau) >= 3 else "narrow_plateau")
    if best <= 0:
        label = "weak_objective"
    return {
        "raw_best_parameter": raw_best,
        "selected_parameter": selected[0],
        "best_sharpe": best,
        "selected_sharpe": selected[1],
        "positive_parameter_fraction": positive,
        "plateau_width": len(plateau),
        "robustness_label": label,
        "default_parameter": default_value,
    }


def assess_stage1(run_df: pd.DataFrame, thresholds: dict[str, Any] | None = None) -> dict[str, Any]:
    thresholds = dict(DEFAULT_THRESHOLDS if not thresholds else {**DEFAULT_THRESHOLDS, **thresholds})
    baseline = _first_row(run_df, "BASELINE_DEV")
    validation = _first_row(run_df, "VALIDATION")
    holdout = _first_row(run_df, "FINAL_HOLDOUT")
    param_sens = filter_test_type(run_df, "PARAM_SENS")
    robustness = parameter_robustness_summary(param_sens)
    wfo = walk_forward_aggregates(run_df, include_holdout=False)

    validation_sharpe = _num(validation, "sharpe_ratio") if validation is not None else None
    is_sharpe = robustness.get("selected_sharpe")
    oos_is = None
    if validation_sharpe is not None and is_sharpe not in (None, 0):
        oos_is = float(validation_sharpe) / float(is_sharpe)

    checks = []

    def add(name: str, passed: bool | None, detail: str) -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})

    if validation_sharpe is None:
        add("validation_sharpe", False, "Validation Sharpe is missing")
    else:
        add(
            "validation_sharpe",
            validation_sharpe > float(thresholds["min_validation_sharpe"]),
            "Validation Sharpe {0:.4f} vs min {1}".format(
                validation_sharpe, thresholds["min_validation_sharpe"]
            ),
        )
    if oos_is is None:
        add("oos_is_sharpe_ratio", False, "OOS/IS Sharpe ratio is missing")
    else:
        add(
            "oos_is_sharpe_ratio",
            oos_is >= float(thresholds["min_oos_is_sharpe_ratio"]),
            "OOS/IS Sharpe ratio {0:.4f} vs min {1}".format(
                oos_is, thresholds["min_oos_is_sharpe_ratio"]
            ),
        )
    pos = robustness.get("positive_parameter_fraction")
    if pos is None:
        add("positive_parameter_fraction", False, "Positive parameter fraction is missing")
    else:
        add(
            "positive_parameter_fraction",
            pos >= float(thresholds["min_positive_parameter_fraction"]),
            "Positive parameter fraction {0:.2%} vs min {1:.2%}".format(
                pos, thresholds["min_positive_parameter_fraction"]
            ),
        )
    add(
        "parameter_neighborhood",
        robustness.get("robustness_label") not in {"isolated_peak", "weak_objective", "insufficient_data", None},
        "Robustness classification: {0}".format(robustness.get("robustness_label")),
    )
    frac = wfo.get("profitable_fraction")
    if frac is None:
        add("wfo_profitable_fraction", False, "No walk-forward OOS tests available")
    else:
        add(
            "wfo_profitable_fraction",
            frac >= float(thresholds["min_profitable_wfo_fraction"]),
            "WFO profitable fraction {0:.2%} vs min {1:.2%}".format(
                frac, thresholds["min_profitable_wfo_fraction"]
            ),
        )
    med = wfo.get("median_oos_sharpe")
    if med is None:
        add("median_wfo_sharpe", False, "Median WFO OOS Sharpe is missing")
    else:
        add(
            "median_wfo_sharpe",
            med >= float(thresholds["min_median_wfo_sharpe"]),
            "Median WFO OOS Sharpe {0:.4f} vs min {1}".format(
                med, thresholds["min_median_wfo_sharpe"]
            ),
        )

    hard = [c for c in checks if c["name"] != "parameter_neighborhood" and c["passed"] is False]
    neighborhood_failed = any(c["name"] == "parameter_neighborhood" and c["passed"] is False for c in checks)
    label = FAIL if hard else (WATCH if neighborhood_failed else PASS)

    return {
        "label": label,
        "checks": checks,
        "baseline": _kpi(baseline),
        "validation": {**_kpi(validation), "oos_is_sharpe_ratio": oos_is},
        "holdout": _kpi(holdout) if holdout is not None else None,
        "robustness": robustness,
        "walk_forward": wfo,
        "thresholds": thresholds,
    }


def _num(row: pd.Series | None, column: str) -> float | None:
    if row is None:
        return None
    value = pd.to_numeric(pd.Series([row.get(column)]), errors="coerce").iloc[0]
    if pd.isna(value):
        return None
    return float(value)


def _kpi(row: pd.Series | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "sharpe": _num(row, "sharpe_ratio"),
        "cagr": _num(row, "cagr"),
        "max_drawdown": _num(row, "max_drawdown"),
        "net_profit": _num(row, "net_profit"),
        "sortino": _num(row, "sortino_ratio"),
        "backtest_id": row.get("backtest_id"),
        "name": row.get("name"),
        "start": row.get("test_start") or row.get("backtest_start"),
        "end": row.get("test_end") or row.get("backtest_end"),
        "parameters": row.get("parameters_json"),
    }


def parse_thresholds_from_row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    raw = None
    if isinstance(row, dict):
        raw = row.get("research_guide_json") or row.get("thresholds")
    else:
        raw = row.get("research_guide_json") if hasattr(row, "get") else None
    if isinstance(raw, dict) and "min_validation_sharpe" in raw:
        return raw
    return None
