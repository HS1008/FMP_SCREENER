"""
One-time cleanup for malformed QuantConnect live snapshots.

A prior bug in jobs/sync_quantconnect.py parse_portfolio() read live
holdings with the wrong field names (quantity/price/marketValue instead
of q/p/v). That made holdings_value = 0 and equity collapse to leftover
cash (~$388) while SPYTrend was still Running with ~$5,000 of SPY.

This script is NOT run by the sync job, cron, or GitHub Actions.
Run it manually after deploying the parser fix.

Conservative rule (does not wipe all history):
  Delete SPYTrend live_snapshots where equity < 1000 AND status = 'Running'.
  Also delete zero-value positions in the same timestamp window.

Usage:
  python scripts/cleanup_bad_qc_snapshots.py            # dry-run
  python scripts/cleanup_bad_qc_snapshots.py --execute
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from db.connection import engine


STRATEGY_MATCH = "SPYTrend"
EQUITY_CEILING = 1000


def _strategy_filter_sql():
    return """
        (st.strategy_id = :strategy_match OR st.name = :strategy_match)
    """


def preview():
    params = {"strategy_match": STRATEGY_MATCH}

    with engine.connect() as conn:
        snapshots = conn.execute(
            text(f"""
                SELECT
                    s.strategy_id,
                    s.timestamp,
                    s.equity,
                    s.cash,
                    s.holdings_value,
                    s.status
                FROM live_snapshots s
                JOIN strategies st
                  ON st.strategy_id = s.strategy_id
                WHERE {_strategy_filter_sql()}
                  AND s.status = 'Running'
                  AND s.equity < :equity_ceiling
                ORDER BY s.timestamp
            """),
            {
                **params,
                "equity_ceiling": EQUITY_CEILING,
            },
        ).mappings().all()

        positions = conn.execute(
            text(f"""
                SELECT
                    p.strategy_id,
                    p.timestamp,
                    p.symbol,
                    p.quantity,
                    p.market_value
                FROM positions p
                JOIN strategies st
                  ON st.strategy_id = p.strategy_id
                WHERE {_strategy_filter_sql()}
                  AND COALESCE(ABS(p.market_value), 0) < 1
                  AND EXISTS (
                      SELECT 1
                      FROM live_snapshots s
                      WHERE s.strategy_id = p.strategy_id
                        AND s.status = 'Running'
                        AND s.equity < :equity_ceiling
                        AND p.timestamp BETWEEN s.timestamp - INTERVAL '5 seconds'
                                            AND s.timestamp + INTERVAL '5 seconds'
                  )
                ORDER BY p.timestamp
            """),
            {
                **params,
                "equity_ceiling": EQUITY_CEILING,
            },
        ).mappings().all()

    return snapshots, positions


def execute_cleanup():
    params = {
        "strategy_match": STRATEGY_MATCH,
        "equity_ceiling": EQUITY_CEILING,
    }

    with engine.begin() as conn:
        deleted_positions = conn.execute(
            text(f"""
                DELETE FROM positions p
                USING strategies st
                WHERE p.strategy_id = st.strategy_id
                  AND {_strategy_filter_sql()}
                  AND COALESCE(ABS(p.market_value), 0) < 1
                  AND EXISTS (
                      SELECT 1
                      FROM live_snapshots s
                      WHERE s.strategy_id = p.strategy_id
                        AND s.status = 'Running'
                        AND s.equity < :equity_ceiling
                        AND p.timestamp BETWEEN s.timestamp - INTERVAL '5 seconds'
                                            AND s.timestamp + INTERVAL '5 seconds'
                  )
            """),
            params,
        ).rowcount

        deleted_snapshots = conn.execute(
            text(f"""
                DELETE FROM live_snapshots s
                USING strategies st
                WHERE s.strategy_id = st.strategy_id
                  AND {_strategy_filter_sql()}
                  AND s.status = 'Running'
                  AND s.equity < :equity_ceiling
            """),
            params,
        ).rowcount

    return deleted_snapshots, deleted_positions


def main():
    parser = argparse.ArgumentParser(
        description=(
            "One-time cleanup of malformed SPYTrend live snapshots "
            "from the prior QuantConnect holdings parser bug."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete rows. Default is dry-run.",
    )
    args = parser.parse_args()

    snapshots, positions = preview()

    print(
        "One-time cleanup for the prior QuantConnect portfolio parser bug."
    )
    print(
        f"Rule: {STRATEGY_MATCH} live_snapshots where "
        f"equity < {EQUITY_CEILING} AND status = 'Running'."
    )
    print(f"Malformed snapshots: {len(snapshots)}")
    print(f"Matching zero-value positions: {len(positions)}")

    if snapshots:
        first = snapshots[0]
        last = snapshots[-1]
        print(
            f"Snapshot window: {first['timestamp']} -> {last['timestamp']}"
        )
        print(
            f"Equity range: ${float(first['equity']):,.2f} "
            f"to ${float(last['equity']):,.2f}"
        )

    if not args.execute:
        print()
        print("Dry-run only. Re-run with --execute to delete these rows.")
        return

    deleted_snapshots, deleted_positions = execute_cleanup()

    print()
    print(f"Deleted live_snapshots: {deleted_snapshots}")
    print(f"Deleted positions: {deleted_positions}")


if __name__ == "__main__":
    main()
