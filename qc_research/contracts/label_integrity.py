"""Pinned CSFML V1 label-integrity bound for the dashboard.

Display only. Does not change economic_gate, holdout, or authorize a rerun.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SNAPSHOT = Path(__file__).resolve().parent / "csfml_v1_label_integrity.json"


def load_csfml_v1_label_integrity() -> dict[str, Any]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def csfml_v1_integrity_caption(
    strategy_id: str | None = None,
    research_run_id: str | None = None,
) -> str | None:
    pin = load_csfml_v1_label_integrity()
    if strategy_id and str(strategy_id) != pin["strategy_id"]:
        return None
    official_run = str(pin.get("full_suite_run_id") or "")
    if research_run_id and official_run and str(research_run_id) != official_run:
        if not str(research_run_id).startswith("STAGE2_CrossSectionalFactorML_"):
            return None
    return str(pin["monitor_caption"])
