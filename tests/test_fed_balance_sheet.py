"""H.4.1 balance-sheet as-of changes and calendar-date labels."""

from __future__ import annotations

from datetime import date

from market_intelligence.display_dates import format_calendar_date
from market_intelligence.fed_balance_sheet import (
    BALANCE_SHEET_SECTIONS,
    absolute_change,
    build_component,
    build_statement,
    column_headers,
    comparison_lines,
    displayed_change,
    format_absolute,
    format_millions,
    format_percent,
    latest_observation,
    normalize_history,
    observation_on_or_before,
    percent_change,
    previous_observation,
    statement_as_of,
    statement_html,
)
from market_intelligence.history_range import align_range_selection
from market_intelligence.macro_dashboard import POLICY_RATES_DEFAULT_START


def _weeks() -> list[tuple[date, float]]:
    return [
        (date(2021, 1, 6), 80.0),
        (date(2023, 1, 4), 90.0),
        (date(2024, 1, 3), 100.0),
        (date(2024, 1, 10), 110.0),
        (date(2024, 1, 17), 120.0),
    ]


def test_latest_and_previous_observation():
    history = normalize_history(
        [
            {"as_of": "2024-01-03", "value": 100},
            {"as_of": "2024-01-10", "value": float("nan")},
            {"as_of": "2024-01-10", "value": 110},
            {"observation_date": "2024-01-17", "value": 120},
        ]
    )
    assert latest_observation(history) == (date(2024, 1, 17), 120.0)
    assert previous_observation(history) == (date(2024, 1, 10), 110.0)
    assert previous_observation(history[:1]) is None
    assert latest_observation([]) is None


def test_one_year_and_three_year_asof_use_current_observation():
    row = build_component("WALCL", "Total Assets", "assets", "Assets", _weeks())
    assert row.current == 120.0
    assert row.current_date == date(2024, 1, 17)
    assert row.year_1 == 90.0
    assert row.year_1_date == date(2023, 1, 4)
    assert row.year_3 == 80.0
    assert row.year_3_date == date(2021, 1, 6)
    assert displayed_change(row, mode="percentage", comparing=False, horizon="1y") == percent_change(120.0, 90.0)
    assert displayed_change(row, mode="absolute", comparing=False, horizon="3y") == absolute_change(120.0, 80.0)


def test_historical_date_uses_latest_observation_on_or_before():
    history = _weeks()
    assert observation_on_or_before(history, date(2024, 1, 12)) == (date(2024, 1, 10), 110.0)
    assert observation_on_or_before(history, date(2024, 1, 10)) == (date(2024, 1, 10), 110.0)
    assert observation_on_or_before(history, date(2020, 12, 30)) is None
    friday = build_component(
        "WALCL",
        "Total Assets",
        "assets",
        "Assets",
        history,
        comparison_date=date(2024, 1, 12),
    )
    assert friday.historical_date == date(2024, 1, 10)
    assert friday.historical == 110.0
    assert friday.current == 120.0
    assert displayed_change(friday, mode="percentage", comparing=True, horizon="lead") == percent_change(120.0, 110.0)
    lines = comparison_lines((friday,), date(2024, 1, 12))
    assert lines[0] == "Comparison: 01/12/2024"
    assert lines[1] == "Using H.4.1 observation: 01/10/2024"


def test_weekly_gap_does_not_take_a_future_print():
    history = [(date(2024, 1, 3), 100.0), (date(2024, 1, 17), 140.0)]
    assert observation_on_or_before(history, date(2024, 1, 10)) == (date(2024, 1, 3), 100.0)
    assert observation_on_or_before(history, date(2024, 1, 16)) == (date(2024, 1, 3), 100.0)


def test_percent_and_absolute_changes_and_invalid_bases():
    assert percent_change(110.0, 100.0) == 10.0
    assert absolute_change(110.0, 100.0) == 10.0
    assert percent_change(50.0, 0.0) is None
    assert absolute_change(50.0, 0.0) == 50.0
    assert percent_change(None, 10.0) is None
    assert percent_change(10.0, None) is None
    assert absolute_change(float("nan"), 10.0) is None
    assert format_percent(None) == "—"
    assert format_percent(0.0) == "+0.00%"
    assert format_absolute(None) == "—"
    assert "0%" not in format_percent(percent_change(50.0, 0.0))
    assert format_millions(6743031) == "6,743,031"
    assert format_absolute(-1250) == "-1,250"
    assert format_absolute(1250) == "+1,250"


def test_missing_component_stays_blank_and_headers_follow_the_toggle():
    missing = build_component("WCSL", "Surplus", "equity", "Equity / Capital", [])
    assert missing.current is None
    assert statement_as_of((missing,)) is None
    assert format_millions(missing.current) == "—"
    assert column_headers(mode="percentage", comparing=False) == (
        "Component",
        "Current",
        "% Change Since Last",
        "% Change 1Y",
        "% Change 3Y",
    )
    assert column_headers(mode="percentage", comparing=True)[2] == "% Change From Current"
    assert column_headers(mode="absolute", comparing=False)[2:] == (
        "Absolute Change Since Last",
        "Absolute Change 1Y",
        "Absolute Change 3Y",
    )
    assert column_headers(mode="absolute", comparing=True)[2] == "Absolute Change From Current"
    html = statement_html((missing,), mode="percentage", comparing=False)
    assert "Surplus" in html
    assert "Equity / Capital" in html
    assert "<th></th>" not in html
    assert "nan" not in html.lower()
    assert "inf" not in html.lower()


def test_statement_sections_and_mixed_observation_dates():
    histories = {
        "WALCL": [{"as_of": date(2024, 1, 10), "value": 1000}, {"as_of": date(2024, 1, 17), "value": 1100}],
        "WCSL": [{"as_of": date(2024, 1, 3), "value": 5}, {"as_of": date(2024, 1, 10), "value": 6}],
    }
    rows = build_statement(histories, comparison_date=date(2024, 1, 12))
    by_id = {row.series_id: row for row in rows}
    assert [section for _sid, section, _rows in BALANCE_SHEET_SECTIONS] == [
        "Assets",
        "Liabilities",
        "Equity / Capital",
    ]
    assert by_id["WALCL"].current_date == date(2024, 1, 17)
    assert by_id["WCSL"].current_date == date(2024, 1, 10)
    assert by_id["TREAST"].current is None
    assert statement_as_of(rows) == date(2024, 1, 17)
    assert by_id["WALCL"].historical_date == date(2024, 1, 10)
    html = statement_html(rows, mode="absolute", comparing=True)
    assert "Assets" in html and "Liabilities" in html and "Equity / Capital" in html
    assert "Total Assets" in html and "Bank Reserves" in html and "Capital Paid In" in html
    assert "1,100" in html
    assert 'class="num pos"' in html or 'class="num neg"' in html


def test_calendar_dates_are_month_day_year():
    assert format_calendar_date(date(2000, 1, 1)) == "01/01/2000"
    assert format_calendar_date("2026-09-30") == "09/30/2026"
    assert format_calendar_date(date(2026, 10, 4)) == "10/04/2026"
    assert format_calendar_date(None) == "—"
    assert format_calendar_date("not-a-date") == "—"


def test_policy_default_start_is_kept_and_earlier_history_stays_selectable():
    earliest = date(1954, 7, 1)
    latest = date(2026, 10, 1)
    assert POLICY_RATES_DEFAULT_START == date(2000, 1, 1)
    default = align_range_selection(
        earliest=earliest,
        latest=latest,
        previous_span=None,
        current_from=None,
        current_to=None,
        default_start=POLICY_RATES_DEFAULT_START,
    )
    assert default == (date(2000, 1, 1), latest)
    earlier = align_range_selection(
        earliest=earliest,
        latest=latest,
        previous_span=(earliest, latest),
        current_from=date(1990, 6, 1),
        current_to=latest,
        default_start=POLICY_RATES_DEFAULT_START,
        previous_default=date(2000, 1, 1),
    )
    assert earlier == (date(1990, 6, 1), latest)
    full = align_range_selection(
        earliest=earliest,
        latest=latest,
        previous_span=(earliest, latest),
        current_from=earliest,
        current_to=latest,
        default_start=POLICY_RATES_DEFAULT_START,
        previous_default=date(2000, 1, 1),
    )
    assert full[0] == earliest
