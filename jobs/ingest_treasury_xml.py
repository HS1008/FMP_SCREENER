"""Scheduled Treasury XML writer.

    python -m jobs.ingest_treasury_xml
    python -m jobs.ingest_treasury_xml --lookback-months 2 --json
"""

from __future__ import annotations

import argparse
import json
import sys

from market_intelligence.ingest_treasury import ingest_treasury
from market_intelligence.treasury_xml import TreasuryXmlClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest Treasury daily XML par yields")
    parser.add_argument("--lookback-months", type=int, default=2)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--as-of", default=None)
    args = parser.parse_args(argv)
    from datetime import date

    from market_intelligence.writer_db import writer_engine

    today = date.fromisoformat(args.as_of) if args.as_of else None
    engine = writer_engine()
    report = ingest_treasury(engine, TreasuryXmlClient(), today=today, lookback_months=args.lookback_months)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, default=str))
    else:
        print("treasury_xml {0} latest={1}".format(report.status, report.latest_observation))
    return 2 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
