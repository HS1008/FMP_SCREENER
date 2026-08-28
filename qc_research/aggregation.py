"""Stage 1 grouping, WFO aggregates, comparison backtest selection, assessment."""

from __future__ import annotations

from typing import Any

import pandas as pd


PASS = "PASS"
WATCH = "WATCH"
FAIL = "FAIL"
IN_PROGRESS = "IN_PROGRESS"
COMPLETE = "COMPLETE"
INCOMPLETE = "INCOMPLETE"

DEFAULT_THRESHOLDS = {
    "min_validation_sharpe": 0.0,
    "min_oos_is_sharpe_ratio": 0.60,
    "min_profitable_wfo_fraction": 0.70,
    "min_median_wfo_sharpe": 0.0,
    "min_positive_parameter_fraction": 0.60,
}

COMPARISON_PRIORITY = ("FINAL_HOLDOUT", "VALIDATION", "BASELINE_DEV")
PRIMARY_EQUITY_TEST_TYPES = ("BASELINE_DEV", "VALIDATION", "WFO_TEST")
EXPECTED_STAGE1_EXPERIMENTS = 81


def smoke_mask(df: pd.DataFrame) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype=bool)
    mask = pd.Series(False, index=df.index)
    if "research_test_type" in df.columns:
        mask = mask | df["research_test_type"].fillna("").astype(str).str.upper().eq("SMOKE")
    if "research_phase" in df.columns:
        mask = mask | df["research_phase"].fillna("").astype(str).str.upper().eq("SMOKE")
    if "name" in df.columns:
        mask = mask | df["name"].fillna("").astype(str).str.contains("__SMOKE__", regex=False)
    return mask


def smoke_backtests(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    return df.loc[smoke_mask(df)].copy()


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
    mask = mask & ~smoke_mask(df)
    return df.loc[mask].copy()


def stage1_backtests(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    stage = df.loc[is_stage1(df)].copy()
    return stage.loc[~smoke_mask(stage)].copy()


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
        primary = row.get("research_primary_parameter")
        if not primary:
            primary = next((k for k in params if k not in {"start_date", "end_date", "starting_cash"}), None)
        if primary and params.get(primary) is not None:
            param_text = " — {0} {1}".format(primary, params.get(primary))
    window = ""
    if start and end:
        window = " — {0}-{1}".format(str(start)[:10], str(end)[:10])
    return "{0}{1}{2}".format(title, window, param_text)


def filter_test_type(df: pd.DataFrame, test_type: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    return df[df["research_test_type"] == test_type].copy()


def walk_forward_aggregates(df: pd.DataFrame, *, include_holdout: bool = False, expected_wfo_windows: int | None = None) -> dict[str, Any]:
    empty = {
        "n_windows": 0,
        "n_profitable": 0,
        "profitable_fraction": None,
        "profitable_completed_fraction": None,
        "profitable_planned_fraction": None,
        "expected_wfo_windows": expected_wfo_windows,
        "completed_wfo_windows": 0,
        "failed_wfo_windows": 0,
        "skipped_wfo_windows": 0,
        "completion_fraction": None,
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

    failed_mask = tests["status"].astype(str).str.lower().str.contains("error|fail", na=False) if "status" in tests.columns else pd.Series([False] * len(tests), index=tests.index)
    skipped_mask = tests["status"].astype(str).str.lower().eq("skipped") if "status" in tests.columns else pd.Series([False] * len(tests), index=tests.index)
    completed = tests[~failed_mask & ~skipped_mask]
    n_failed = int(failed_mask.sum())
    n_skipped = int(skipped_mask.sum())
    n_completed = int(len(completed))
    planned = expected_wfo_windows if expected_wfo_windows else int(len(tests))

    sharpes = pd.to_numeric(completed["sharpe_ratio"], errors="coerce").dropna() if n_completed else pd.Series(dtype=float)
    cagrs = pd.to_numeric(completed["cagr"], errors="coerce").dropna() if n_completed and "cagr" in completed.columns else pd.Series(dtype=float)
    dds = pd.to_numeric(completed["max_drawdown"], errors="coerce").dropna() if n_completed and "max_drawdown" in completed.columns else pd.Series(dtype=float)
    nets = pd.to_numeric(completed["net_profit"], errors="coerce") if n_completed and "net_profit" in completed.columns else pd.Series(dtype=float)
    profitable = int((nets.fillna(0) > 0).sum()) if n_completed and nets.notna().any() else int((sharpes > 0).sum()) if n_completed else 0
    history = []
    for _, row in completed.iterrows():
        summary = row.get("research_selection_summary_json")
        if isinstance(summary, dict) and summary.get("selected_parameter") is not None:
            history.append(summary.get("selected_parameter"))
            continue
        params = row.get("parameters_json") or {}
        if isinstance(params, dict):
            primary = (
                row.get("research_primary_parameter")
                or (summary or {}).get("primary_parameter")
                if isinstance(summary, dict)
                else row.get("research_primary_parameter")
            )
            if not primary:
                primary = next((k for k in params if k not in {"start_date", "end_date", "starting_cash"}), None)
            history.append(params.get(primary) if primary else None)
        else:
            history.append(None)
    unique = sorted({str(v) for v in history if v is not None})
    stability = None
    if history:
        stability = 1.0 - ((len(unique) - 1) / float(max(len(history), 1)))
    return {
        "n_windows": n_completed,
        "n_profitable": profitable,
        "profitable_fraction": profitable / float(n_completed) if n_completed else None,
        "profitable_completed_fraction": profitable / float(n_completed) if n_completed else None,
        "profitable_planned_fraction": profitable / float(planned) if planned else None,
        "expected_wfo_windows": planned,
        "completed_wfo_windows": n_completed,
        "failed_wfo_windows": n_failed,
        "skipped_wfo_windows": n_skipped,
        "completion_fraction": n_completed / float(planned) if planned else None,
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


def _parse_json_value(value: Any) -> dict[str, Any]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            import json

            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def authoritative_selection_summary(run_df: pd.DataFrame) -> dict[str, Any] | None:
    """Prefer orchestrator selection metadata on VALIDATION (then WFO_TEST)."""
    for test_type in ("VALIDATION", "WFO_TEST"):
        matches = filter_test_type(run_df, test_type)
        if matches is None or matches.empty:
            continue
        if "research_selection_summary_json" not in matches.columns:
            continue
        for _, row in matches.iterrows():
            parsed = _parse_json_value(row.get("research_selection_summary_json"))
            if parsed.get("selected_parameter") is not None or parsed.get("primary_parameter"):
                parsed = dict(parsed)
                parsed["authoritative"] = True
                parsed["source"] = parsed.get("source") or "orchestrator"
                if "best_sharpe" not in parsed and parsed.get("best_objective") is not None:
                    parsed["best_sharpe"] = parsed.get("best_objective")
                if "selected_sharpe" not in parsed and parsed.get("selected_objective") is not None:
                    parsed["selected_sharpe"] = parsed.get("selected_objective")
                return parsed
    return None


def parameter_robustness_summary(
    param_sens: pd.DataFrame,
    *,
    primary: str | None = None,
    default_value: Any = None,
    run_df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Display selection. Orchestrator summary is authoritative.

    Local recomputation is a documented legacy fallback only, used when no
    research_selection_summary was persisted. It must not invent a primary
    parameter of sma_period unless that is stored on the run.
    """
    authoritative = authoritative_selection_summary(run_df if run_df is not None else param_sens)
    if authoritative:
        return authoritative

    resolved_primary = primary
    if not resolved_primary and param_sens is not None and not param_sens.empty:
        if "research_primary_parameter" in param_sens.columns:
            values = param_sens["research_primary_parameter"].dropna()
            if not values.empty:
                resolved_primary = values.iloc[0]
        if not resolved_primary:
            sample = param_sens.iloc[0].get("parameters_json") or {}
            if isinstance(sample, dict):
                resolved_primary = next(
                    (k for k in sample if k not in {"start_date", "end_date", "starting_cash"}),
                    None,
                )
    if not resolved_primary:
        return {
            "raw_best_parameter": None,
            "selected_parameter": None,
            "best_sharpe": None,
            "selected_sharpe": None,
            "positive_parameter_fraction": None,
            "plateau_width": None,
            "robustness_label": "insufficient_data",
            "authoritative": False,
            "source": "legacy_fallback",
            "primary_parameter": None,
        }

    if param_sens is None or param_sens.empty:
        return {
            "raw_best_parameter": None,
            "selected_parameter": None,
            "best_sharpe": None,
            "selected_sharpe": None,
            "positive_parameter_fraction": None,
            "plateau_width": None,
            "robustness_label": "insufficient_data",
            "authoritative": False,
            "source": "legacy_fallback",
            "primary_parameter": resolved_primary,
        }
    rows = []
    for _, row in param_sens.iterrows():
        params = row.get("parameters_json") or {}
        if not isinstance(params, dict):
            continue
        try:
            param = float(params.get(resolved_primary))
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
            "authoritative": False,
            "source": "legacy_fallback",
            "primary_parameter": resolved_primary,
        }
    rows.sort(key=lambda item: item[0])
    sharpes = [item[1] for item in rows]
    best = max(sharpes)
    default_number = 0.0
    try:
        default_number = float(default_value) if default_value is not None else rows[0][0]
    except (TypeError, ValueError):
        default_number = rows[0][0]
    raw_best = min((item for item in rows if item[1] == best), key=lambda item: abs(item[0] - default_number))[0]
    positive = sum(1 for value in sharpes if value > 0) / float(len(sharpes))
    threshold = 0.90 * best if best > 0 else best
    plateau = [item for item in rows if item[1] >= threshold]
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
            -neighbor_mean(item[0]),
            abs(item[0] - default_number),
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
        "primary_parameter": resolved_primary,
        "authoritative": False,
        "source": "legacy_fallback",
        "note": (
            "Legacy fallback: recomputed locally because no orchestrator "
            "research_selection_summary was stored. Do not treat this as "
            "authoritative for a new strategy."
        ),
    }


def assess_stage1(
    run_df: pd.DataFrame,
    thresholds: dict[str, Any] | None = None,
    research_run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if run_df is not None and not run_df.empty:
        run_df = run_df.loc[~smoke_mask(run_df)].copy()
    thresholds = dict(DEFAULT_THRESHOLDS if not thresholds else {**DEFAULT_THRESHOLDS, **thresholds})
    baseline = _first_row(run_df, "BASELINE_DEV")
    validation = _first_row(run_df, "VALIDATION")
    holdout = _first_row(run_df, "FINAL_HOLDOUT")
    param_sens = filter_test_type(run_df, "PARAM_SENS")
    robustness = parameter_robustness_summary(param_sens, run_df=run_df)
    run_meta = dict(research_run or {})
    expected_wfo = None
    wfo = walk_forward_aggregates(run_df, include_holdout=False, expected_wfo_windows=expected_wfo)

    expected = None
    if run_meta.get("expected_experiment_count") is not None:
        expected = int(run_meta.get("expected_experiment_count"))
    synced = int(len(run_df)) if run_df is not None else 0
    completed = 0
    failed = 0
    skipped = 0
    if run_df is not None and not run_df.empty and "status" in run_df.columns:
        status = run_df["status"].astype(str).str.lower()
        completed = int(status.str.contains("completed").sum())
        failed = int(status.str.contains("error|fail").sum())
        skipped = int(status.eq("skipped").sum())
    if run_meta.get("skipped_count") is not None:
        skipped = max(skipped, int(run_meta.get("skipped_count") or 0))
    if run_meta.get("completed_count") is not None and run_meta.get("expected_experiment_count") is not None:
        completed = max(completed, int(run_meta.get("completed_count") or 0))
    if run_meta.get("failed_count") is not None:
        failed = max(failed, int(run_meta.get("failed_count") or 0))
    if run_meta.get("synced_experiment_count") is not None:
        synced = max(synced, int(run_meta.get("synced_experiment_count") or 0))

    authoritative_status = str(run_meta.get("run_status") or "")
    if authoritative_status == IN_PROGRESS:
        run_status = IN_PROGRESS
    elif authoritative_status in {COMPLETE, INCOMPLETE}:
        run_status = authoritative_status
    elif expected and (completed + failed + skipped) < expected:
        run_status = IN_PROGRESS
    elif expected and (failed or skipped or completed < expected):
        run_status = INCOMPLETE
    elif expected and completed >= expected:
        run_status = COMPLETE
    else:
        run_status = IN_PROGRESS if expected else COMPLETE

    validation_sharpe = _num(validation, "sharpe_ratio") if validation is not None else None
    is_sharpe = robustness.get("selected_sharpe") or robustness.get("selected_objective")
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
        robustness.get("robustness_label")
        not in {
            "isolated_peak",
            "weak_objective",
            "insufficient_data",
            "incomplete_grid",
            "multi_dim_best_objective_only",
            None,
        },
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
    if run_status == IN_PROGRESS:
        label = IN_PROGRESS
    elif run_status == INCOMPLETE:
        label = INCOMPLETE
    else:
        label = FAIL if hard else (WATCH if neighborhood_failed else PASS)
        if robustness.get("robustness_label") == "multi_dim_best_objective_only" and label == PASS:
            label = WATCH

    return {
        "label": label,
        "run_status": run_status,
        "progress": {
            "expected_experiment_count": expected,
            "synced_experiment_count": synced,
            "completed_count": completed,
            "failed_count": failed,
            "skipped_count": skipped,
        },
        "checks": checks,
        "baseline": _kpi(baseline),
        "validation": {**(_kpi(validation) or {}), "oos_is_sharpe_ratio": oos_is},
        "holdout": _kpi(holdout) if holdout is not None else None,
        "robustness": robustness,
        "walk_forward": wfo,
        "thresholds": thresholds,
        "note": (
            "PASS/WATCH/FAIL is assigned only to COMPLETE suites. "
            "In-progress research is not FAIL. Holdout is not used for the label. "
            "research_runs is authoritative for expected count and run_status."
        ),
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
        "trade_count": row.get("trade_count"),
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
        raw = row.get("research_thresholds_json") or row.get("thresholds") or row.get("research_guide_json")
    else:
        raw = None
        if hasattr(row, "get"):
            raw = row.get("research_thresholds_json")
            if not raw:
                raw = row.get("thresholds")
            if not raw:
                # research_guide_json is QuantConnect researchGuide, not Stage 1 thresholds.
                candidate = row.get("research_guide_json")
                parsed = candidate if isinstance(candidate, dict) else {}
                if "min_validation_sharpe" in parsed:
                    raw = parsed
    if isinstance(raw, str):
        raw = _parse_json_value(raw)
    if isinstance(raw, dict) and "min_validation_sharpe" in raw:
        return raw
    return None


def parse_orchestrator_summary(research_run: dict[str, Any] | None) -> dict[str, Any]:
    if not research_run:
        return {}
    raw = research_run.get("orchestrator_summary_json")
    if isinstance(raw, dict):
        return raw
    return _parse_json_value(raw)


def skipped_experiment_rows(summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    payload = summary or {}
    rows = []
    run_id = payload.get("research_run_id")
    for item in payload.get("skipped_experiments") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "backtest_id": None,
                "name": item.get("name"),
                "status": "skipped",
                "research_suite_version": "S1",
                "research_run_id": run_id,
                "research_experiment_id": item.get("experiment_id"),
                "research_test_type": item.get("test_type"),
                "research_window_id": item.get("window_id"),
                "research_phase": item.get("phase") or item.get("test_type"),
                "error_message": item.get("error"),
                "research_is_holdout": False,
                "parameters_json": {},
            }
        )
    return rows


def attach_skipped_experiments(
    run_df: pd.DataFrame | None,
    summary: dict[str, Any] | None,
) -> pd.DataFrame:
    extra = skipped_experiment_rows(summary)
    if run_df is None:
        run_df = pd.DataFrame()
    if not extra:
        return run_df.copy() if run_df is not None else pd.DataFrame()
    existing_ids = set()
    if not run_df.empty and "research_experiment_id" in run_df.columns:
        existing_ids = {
            str(value)
            for value in run_df["research_experiment_id"].dropna().astype(str).tolist()
        }
    new_rows = [
        row
        for row in extra
        if not row.get("research_experiment_id")
        or str(row.get("research_experiment_id")) not in existing_ids
    ]
    if not new_rows:
        return run_df.copy()
    return pd.concat([run_df, pd.DataFrame(new_rows)], ignore_index=True)


def primary_equity_backtests(run_df: pd.DataFrame | None) -> pd.DataFrame:
    if run_df is None or run_df.empty:
        return pd.DataFrame()
    if "research_test_type" not in run_df.columns:
        return pd.DataFrame()
    subset = run_df[run_df["research_test_type"].isin(PRIMARY_EQUITY_TEST_TYPES)].copy()
    if "status" in subset.columns:
        skipped = subset["status"].astype(str).str.lower().eq("skipped")
        subset = subset[~skipped]
    return subset


def research_date_range(run_df: pd.DataFrame | None) -> tuple[str | None, str | None]:
    if run_df is None or run_df.empty:
        return None, None
    starts = []
    ends = []
    for column in ("test_start", "backtest_start", "train_start"):
        if column in run_df.columns:
            starts.extend(str(value)[:10] for value in run_df[column].dropna().tolist())
    for column in ("test_end", "backtest_end", "train_end"):
        if column in run_df.columns:
            ends.extend(str(value)[:10] for value in run_df[column].dropna().tolist())
    starts = [value for value in starts if value and value != "NaT"]
    ends = [value for value in ends if value and value != "NaT"]
    return (min(starts) if starts else None, max(ends) if ends else None)


def accessed_2023_or_later(run_df: pd.DataFrame | None) -> bool:
    _start, end = research_date_range(run_df)
    if not end:
        return False
    return end >= "2023-01-01"
