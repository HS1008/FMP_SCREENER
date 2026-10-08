"""Publish the Market Overview snapshot once. Streamlit does not run this module.

    python -m jobs.publish_overview_snapshot

jobs.yahoo_dashboard_quotes also publishes after each successful collection, so
this entry point is for a manual refresh or a one-off after a deploy.
"""

from __future__ import annotations

import logging
import sys

from market_intelligence.overview_publish import publish_overview_snapshot
from market_intelligence.writer_db import WriterConfigurationError, writer_engine

logger = logging.getLogger("jobs.publish_overview_snapshot")


def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        engine = writer_engine()
    except WriterConfigurationError as exc:
        logger.error("overview snapshot has no writer database: %s", exc.__class__.__name__)
        return 3
    try:
        with engine.begin() as conn:
            result = publish_overview_snapshot(conn)
    except Exception:
        logger.exception("overview snapshot publish failed")
        return 2
    logger.info("published %s (%s bytes)", result["snapshot_id"], result["payload_bytes"])
    return 0


if __name__ == "__main__":
    sys.exit(run())
