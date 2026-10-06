"""Fill ``assets/Market_Overview_Template.xlsx`` from a frozen overview snapshot.

The template is opened with openpyxl and only cell *values* and *number formats*
are written, so sheet name, placement, fonts, fills, column widths, and row
heights stay as authored. The workbook has no formulas, merged cells, or
defined names, so there is no cached-formula result to go stale.

Cell conventions (documented for the user, not inferred by them):

* Column A keeps the template's "<label> - Current Price" idea while staying
  numeric: the cell value is the level and the label lives in the number
  format (``"SPY - "0.00``). A missing level is written as the text
  ``"<label> - n/a"`` so a blank is never mistaken for zero.
* Column B is a real Excel date (observation date of the stored close or
  metric). Stored data is end-of-day; no intraday time is fabricated.
* Percentage returns are numeric fractions with a ``0.00%`` format. Basis-point
  moves are plain numbers with a signed integer format. Missing values are
  empty cells.
* Export metadata (export time in America/New_York, snapshot generation time
  in UTC, snapshot id, observation-date span, conventions) sits in column K,
  outside the A:I data range.
"""

from __future__ import annotations

import io
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

from market_intelligence.overview_snapshot import (
    SECTION_COMMODITIES,
    SECTION_CREDIT,
    SECTION_CRYPTO,
    SECTION_FOREX,
    SECTION_GLOBAL,
    SECTION_MARKET_RATIOS,
    SECTION_SECTORS,
    SECTION_US_INDEXES,
    SECTION_VIX_TERM,
    SECTION_YIELD_CURVE,
    section_by_id,
)

TEMPLATE_PATH = Path(__file__).resolve().parent / "assets" / "Market_Overview_Template.xlsx"
TEMPLATE_SHEET = "Market Overview"
EXCEL_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXPORT_TZ = ZoneInfo("America/New_York")
METADATA_COLUMN = "K"

PERCENT_FORMAT = "0.00%"
BPS_FORMAT = '+0" bps";-0" bps";0" bps"'
DATE_FORMAT = "dd/mm/yyyy"
SHARPE_FORMAT = "0.00"
MISSING_TEXT = "n/a"

# Template row for every snapshot row key, per section. Column A/B/C.. placement
# is fixed by the template; the description rows beneath index ETFs are left as is.
TEMPLATE_ROWS: dict[str, dict[str, int]] = {
    SECTION_US_INDEXES: {"SPY": 2, "RSP": 4, "IWM": 6, "QQQ": 8, "DIA": 10},
    SECTION_MARKET_RATIOS: {"RSP_SPY": 14, "IWM_SPY": 15, "QQQ_SPY": 16, "DIA_SPY": 17},
    SECTION_SECTORS: {
        "XLC": 20,
        "XLY": 21,
        "XLP": 22,
        "XLE": 23,
        "XLF": 24,
        "XLV": 25,
        "XLI": 26,
        "XLB": 27,
        "XLRE": 28,
        "XLK": 29,
        "XLU": 30,
    },
    SECTION_YIELD_CURVE: {
        "3M": 33,
        "6M": 34,
        "1Y": 35,
        "2Y": 36,
        "3Y": 37,
        "5Y": 38,
        "7Y": 39,
        "10Y": 40,
        "20Y": 41,
        "30Y": 42,
        "2s10s": 43,
        "2s5s10s": 44,
        "MOVE": 45,
    },
    SECTION_CREDIT: {"BAMLC0A0CM": 48, "BAMLH0A0HYM2": 49, "BAMLEMCBPIOAS": 50},
    SECTION_VIX_TERM: {"VIX_1M": 53, "VIX_3M": 54, "VIX_6M": 55, "VIX_1Y": 56},
    SECTION_GLOBAL: {"SPY": 59, "VEA": 61, "VGK": 63, "EWJ": 65, "VWO": 67, "MCHI": 69, "INDA": 71, "EWZ": 73},
    SECTION_COMMODITIES: {"CL": 77, "BZ": 78, "NG": 79, "GC": 80, "SI": 81, "HG": 82, "ZC": 83, "ZW": 84, "ZS_F": 85},
    SECTION_FOREX: {"EURUSD": 88, "GBPUSD": 89, "USDJPY": 90, "AUDUSD": 91, "USDCAD": 92, "USDCHF": 93, "USDCNH": 94},
    SECTION_CRYPTO: {"BTC": 97, "ETH": 98},
}
TEMPLATE_HEADER_ROWS: dict[str, int] = {
    SECTION_US_INDEXES: 1,
    SECTION_MARKET_RATIOS: 13,
    SECTION_SECTORS: 19,
    SECTION_YIELD_CURVE: 32,
    SECTION_CREDIT: 47,
    SECTION_VIX_TERM: 52,
    SECTION_GLOBAL: 58,
    SECTION_COMMODITIES: 76,
    SECTION_FOREX: 87,
    SECTION_CRYPTO: 96,
}
CHANGE_COLUMNS: tuple[str, ...] = ("C", "D", "E", "F", "G", "H")
RISK_COLUMNS: tuple[str, ...] = ("G", "H", "I")

_LEVEL_DECIMALS: dict[str, int] = {"price": 2, "ratio": 4, "yield_pct": 2, "bps": 0, "index": 2, "fx": 4}


def export_filename(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(EXPORT_TZ)).astimezone(EXPORT_TZ)
    return "Market_Overview_{0}_ET.xlsx".format(stamp.strftime("%Y-%m-%d_%H%M"))


def _escape_format_text(text: str) -> str:
    return text.replace('"', "'")


def level_number_format(label: str, level_kind: str) -> str:
    """Numeric column-A format that carries the instrument label as literal text."""
    prefix = '"{0} - "'.format(_escape_format_text(label))
    decimals = _LEVEL_DECIMALS.get(level_kind, 2)
    digits = "0" if decimals == 0 else "0." + "0" * decimals
    if level_kind == "yield_pct":
        return prefix + digits + '"%"'
    if level_kind == "bps":
        return prefix + digits + '" bps"'
    return prefix + digits


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _write_number(cell, value: Any, number_format: str) -> None:
    if value is None:
        cell.value = None
        return
    cell.value = float(value)
    cell.number_format = number_format


def _row_changes(section: Mapping[str, Any], row: Mapping[str, Any]) -> tuple[Mapping[str, Any], str]:
    """Values and format for the change columns following the template header units."""
    row_kind = row.get("change_kind")
    if row_kind == "bps":
        # Spreads (2s10s, 2s5s10s) stay in bps: a percent change of a spread that
        # crosses zero is meaningless, so the cell carries its own " bps" unit.
        return (row.get("changes") or {}), BPS_FORMAT
    export_kind = row_kind or section.get("export_change_kind") or section.get("change_kind")
    if export_kind == "fraction" and section.get("change_kind") == "bps":
        # Template header says %Change for this section; the snapshot carries the
        # fractional change of the level alongside the bps move.
        return (row.get("changes_pct") or row.get("changes") or {}), PERCENT_FORMAT
    if export_kind == "bps":
        return (row.get("changes") or {}), BPS_FORMAT
    return (row.get("changes") or {}), PERCENT_FORMAT


def fill_template(snapshot: Mapping[str, Any], *, exported_at: datetime | None = None, template_path: Path = TEMPLATE_PATH) -> bytes:
    """Return the filled workbook as bytes. Nothing is written to disk or PostgreSQL."""
    workbook = load_workbook(template_path)
    sheet = workbook[TEMPLATE_SHEET]
    for section_id, rows in TEMPLATE_ROWS.items():
        section = section_by_id(snapshot, section_id)
        if section is None:
            continue
        by_key = {str(row.get("key")): row for row in section.get("rows") or []}
        level_kind = str(section.get("level_kind") or "price")
        columns: Sequence[str] = section.get("change_columns") or []
        for key, excel_row in rows.items():
            row = by_key.get(key)
            label = str((row or {}).get("label") or key)
            level_cell = sheet["A{0}".format(excel_row)]
            if row is None or row.get("level") is None:
                level_cell.value = "{0} - {1}".format(label, MISSING_TEXT)
            else:
                level_cell.value = float(row["level"])
                level_cell.number_format = level_number_format(label, str(row.get("level_kind") or level_kind))
            observed = _as_date((row or {}).get("as_of"))
            date_cell = sheet["B{0}".format(excel_row)]
            if observed is None:
                date_cell.value = None
            else:
                date_cell.value = observed
                date_cell.number_format = DATE_FORMAT
            if row is None:
                continue
            changes, change_format = _row_changes(section, row)
            for column, label_name in zip(CHANGE_COLUMNS, columns):
                _write_number(sheet["{0}{1}".format(column, excel_row)], changes.get(label_name), change_format)
            if section.get("risk"):
                risk = row.get("risk") or {}
                _write_number(sheet["G{0}".format(excel_row)], risk.get("vol_ann"), PERCENT_FORMAT)
                _write_number(sheet["H{0}".format(excel_row)], risk.get("sharpe"), SHARPE_FORMAT)
                _write_number(sheet["I{0}".format(excel_row)], risk.get("max_dd"), PERCENT_FORMAT)
    _write_metadata(sheet, snapshot, exported_at=exported_at)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _write_metadata(sheet, snapshot: Mapping[str, Any], *, exported_at: datetime | None) -> None:
    stamp = (exported_at or datetime.now(EXPORT_TZ)).astimezone(EXPORT_TZ)
    generated = snapshot.get("generated_at")
    lines: list[tuple[str, Any]] = [
        ("Export metadata", None),
        ("Exported (America/New_York)", stamp.strftime("%Y-%m-%d %H:%M %Z")),
        ("Snapshot generated (UTC)", generated),
        ("Snapshot id", snapshot.get("snapshot_id")),
        ("Snapshot version", snapshot.get("snapshot_version")),
        ("Observation dates", "{0} to {1}".format(snapshot.get("as_of_min") or MISSING_TEXT, snapshot.get("as_of_max") or MISSING_TEXT)),
        ("Rows missing / stale", "{0} / {1} of {2}".format(snapshot.get("rows_missing"), snapshot.get("rows_stale"), snapshot.get("rows_total"))),
        ("Missing policy", snapshot.get("missing_policy")),
        ("Column A", "Numeric level; the instrument label is carried in the number format. '<label> - n/a' marks a missing level."),
        ("Column B", "Observation date of the stored end-of-day close or metric (real Excel date). No intraday time is implied."),
        ("Returns", "Percentage returns are fractions formatted 0.00%. Credit Spreads are basis-point moves. Yield Curve %Change is the fractional change of the yield/spread level; MOVE is a percent move of the index."),
        ("Risk", "Volatility Annualized 3M, Sharpe 3M (rf = 0), Max DD 3M over 63 stored daily returns (90 calendar days, 365 annualization, for crypto)."),
        ("Crypto windows", "1D/1W/1M/3M are 1, 7, 30, and 90 calendar days."),
        ("USD/CNH", "CME USD/Offshore RMB future (Yahoo CNH=F); offshore CNH per USD, rising = USD appreciation. Not onshore CNY."),
        ("MOVE", "ICE BofA MOVE via Yahoo ^MOVE, end-of-day index points."),
    ]
    errors = snapshot.get("read_errors") or {}
    if errors:
        lines.append(("Read errors", ", ".join("{0}: {1}".format(key, value) for key, value in sorted(errors.items()))))
    for offset, (label, value) in enumerate(lines, start=1):
        sheet["{0}{1}".format(METADATA_COLUMN, offset)].value = label
        if value is not None:
            sheet["{0}{1}".format(chr(ord(METADATA_COLUMN) + 1), offset)].value = value


__all__ = [
    "BPS_FORMAT",
    "DATE_FORMAT",
    "EXCEL_MIME",
    "EXPORT_TZ",
    "METADATA_COLUMN",
    "PERCENT_FORMAT",
    "TEMPLATE_HEADER_ROWS",
    "TEMPLATE_PATH",
    "TEMPLATE_ROWS",
    "TEMPLATE_SHEET",
    "export_filename",
    "fill_template",
    "level_number_format",
]
