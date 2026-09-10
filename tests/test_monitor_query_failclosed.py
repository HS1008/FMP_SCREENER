"""Research loaders must not treat a down database as an empty library."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError


ROOT = Path(__file__).resolve().parents[1]
MONITOR = (ROOT / "pages" / "strategy_monitor.py").read_text(encoding="utf-8")
QUERIES = (ROOT / "qc_research" / "read_models" / "monitor_queries.py").read_text(encoding="utf-8")
LIBRARY = (ROOT / "qc_research" / "research_library.py").read_text(encoding="utf-8")


def _operational_error() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("connection refused"))


def _programming_error() -> ProgrammingError:
    return ProgrammingError("SELECT 1", {}, Exception("column does not exist"))


def test_monitor_page_refuses_to_treat_query_failure_as_empty_library():
    assert "will not treat a query failure as an empty research library" in MONITOR
    assert "A query failure is not treated as an empty library" in MONITOR
    assert "A query failure is not treated as an empty run list" in MONITOR
    assert "A query failure is not treated as missing research" in MONITOR
    assert "except Exception:\n        return False" not in MONITOR.split("def strategy_has_platform_research", 1)[1]


def test_research_sql_helpers_do_not_swallow_generic_errors():
    assert "except Exception:\n        return pd.DataFrame()" not in QUERIES
    assert "except Exception:\n        return None" not in QUERIES
    assert "except Exception:\n        return pd.DataFrame()" not in LIBRARY
    assert "read_sql_optional" in QUERIES
    assert "Paper/live tables stay optional" in QUERIES


def test_read_sql_raises_operational_error(monkeypatch):
    from qc_research.read_models import monitor_queries as mq

    monkeypatch.setattr(
        mq.pd,
        "read_sql",
        lambda *args, **kwargs: (_ for _ in ()).throw(_operational_error()),
    )
    with pytest.raises(OperationalError):
        mq.read_sql(object(), "SELECT 1")


def test_read_sql_missing_engine_stays_empty():
    from qc_research.read_models.monitor_queries import read_sql

    assert read_sql(None, "SELECT 1").empty


def test_read_sql_optional_swallows_sqlalchemy_errors(monkeypatch):
    from qc_research.read_models import monitor_queries as mq

    monkeypatch.setattr(
        mq.pd,
        "read_sql",
        lambda *args, **kwargs: (_ for _ in ()).throw(_operational_error()),
    )
    assert mq.read_sql_optional(object(), "SELECT 1").empty


def test_read_sql_allow_missing_relation_only_swallows_programming_error(monkeypatch):
    from qc_research.read_models import monitor_queries as mq

    monkeypatch.setattr(
        mq.pd,
        "read_sql",
        lambda *args, **kwargs: (_ for _ in ()).throw(_programming_error()),
    )
    assert mq.read_sql_allow_missing_relation(object(), "SELECT 1").empty

    monkeypatch.setattr(
        mq.pd,
        "read_sql",
        lambda *args, **kwargs: (_ for _ in ()).throw(_operational_error()),
    )
    with pytest.raises(OperationalError):
        mq.read_sql_allow_missing_relation(object(), "SELECT 1")


def test_load_strategies_frame_raises_when_strategies_query_fails(monkeypatch):
    from qc_research.read_models import monitor_queries as mq

    monkeypatch.setattr(
        mq,
        "read_sql",
        lambda *args, **kwargs: (_ for _ in ()).throw(_operational_error()),
    )
    with pytest.raises(OperationalError):
        mq.load_strategies_frame(object())


def test_load_backtests_frame_raises_when_fallback_query_fails(monkeypatch):
    from qc_research.read_models import monitor_queries as mq

    monkeypatch.setattr(mq, "read_sql_allow_missing_relation", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        mq,
        "read_sql",
        lambda *args, **kwargs: (_ for _ in ()).throw(_operational_error()),
    )
    with pytest.raises(OperationalError):
        mq.load_backtests_frame(object(), "SPYTrend")


def test_paper_loaders_stay_empty_on_sql_errors(monkeypatch):
    from qc_research.read_models import monitor_queries as mq

    monkeypatch.setattr(mq, "read_sql_optional", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(mq, "_execute_one_optional", lambda *args, **kwargs: None)
    assert mq.load_equity_history_frame(object(), "SPYTrend").empty
    assert mq.load_orders_frame(object(), "SPYTrend").empty
    assert mq.load_trades_frame(object(), "SPYTrend").empty
    assert mq.load_latest_positions_frame(object(), "SPYTrend").empty
    assert mq.load_latest_snapshot_row(object(), "SPYTrend") is None


def test_research_library_raises_when_runs_query_fails(monkeypatch):
    from qc_research import research_library as lib

    monkeypatch.setattr(
        lib,
        "_read_sql",
        lambda *args, **kwargs: (_ for _ in ()).throw(_operational_error()),
    )
    with pytest.raises(OperationalError):
        lib.load_research_library(object())


def test_research_library_missing_engine_stays_empty():
    from qc_research.research_library import load_research_library, load_strategy_runs

    assert load_research_library(None).empty
    assert load_strategy_runs(None, "SPYTrend").empty


def test_execute_one_raises_operational_error():
    from qc_research.read_models.monitor_queries import _execute_one

    engine = MagicMock()
    engine.connect.side_effect = _operational_error()
    with pytest.raises(OperationalError):
        _execute_one(engine, "SELECT 1")
