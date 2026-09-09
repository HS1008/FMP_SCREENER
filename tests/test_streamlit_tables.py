"""Arrow-safe Strategy Monitor tables (no QuantConnect, no live IBKR)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from qc_research.streamlit_tables import arrow_safe_frame


ROOT = Path(__file__).resolve().parents[1]
MONITOR_UI = (ROOT / "qc_research" / "monitor_ui.py").read_text(encoding="utf-8")


def _mixed_backtest_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Metric": ["CAGR", "Trades"],
            "Backtest": ["12.00%", np.int64(81)],
            "Paper": ["—", 3],
        }
    )


def test_mixed_string_and_int64_column_breaks_arrow():
    with pytest.raises(Exception, match="int64|Expected bytes|Conversion failed"):
        pa.Table.from_pandas(_mixed_backtest_frame())


def test_arrow_safe_frame_stringifies_numpy_int64():
    safe = arrow_safe_frame(_mixed_backtest_frame())
    assert list(safe["Backtest"]) == ["12.00%", "81"]
    assert list(safe["Paper"]) == ["—", "3"]
    table = pa.Table.from_pandas(safe)
    assert table.column("Backtest").to_pylist() == ["12.00%", "81"]


def test_backtest_vs_paper_does_not_pass_raw_trade_count():
    assert 'row.get("trade_count")' in MONITOR_UI
    assert "arrow_safe_frame(comparison)" in MONITOR_UI
    assert 'len(trades) if trades is not None else "—"' not in MONITOR_UI


def test_apptest_renders_sanitized_mixed_column(tmp_path):
    script = tmp_path / "mixed_table_app.py"
    script.write_text(
        "import numpy as np\n"
        "import pandas as pd\n"
        "import streamlit as st\n"
        "from qc_research.streamlit_tables import arrow_safe_frame\n"
        "st.dataframe(\n"
        "    arrow_safe_frame(pd.DataFrame({\n"
        "        'Backtest': ['12.00%', np.int64(81)],\n"
        "        'Paper': ['—', 3],\n"
        "    })),\n"
        "    hide_index=True,\n"
        ")\n",
        encoding="utf-8",
    )
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(script), default_timeout=30)
    at.run()
    assert not at.exception
