"""Persist OpenFIGI mapping results. Transient failures are not cached as NO_MATCH."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import json

from sqlalchemy import text

from market_intelligence.openfigi_client import CATALOG_VERSION, OPENFIGI_SOURCE_ID
from market_intelligence.store import utcnow


def persist_mapping_results(conn, results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"MATCH": 0, "NO_MATCH": 0, "AMBIGUOUS": 0, "VALIDATION_FAILURE": 0, "TRANSIENT_FAILURE": 0}
    retrieved = utcnow()
    for item in results:
        status = str(item.get("status") or "TRANSIENT_FAILURE")
        counts[status] = counts.get(status, 0) + 1
        if status == "TRANSIENT_FAILURE":
            continue
        job = item.get("job") or {}
        conn.execute(
            text(
                """
                INSERT INTO mi_openfigi_cache (
                    request_hash, id_type, id_value, exch_code, currency, market_sec_des, status,
                    figi, composite_figi, share_class_figi, ticker, name, security_type,
                    catalog_version, retrieved_at, expires_at, response_hash
                ) VALUES (
                    :hash, :id_type, :id_value, :exch, :ccy, :msd, :status,
                    :figi, :comp, :share, :ticker, :name, :stype,
                    :catalog, :retrieved, CAST(:expires AS TIMESTAMPTZ), :resp
                )
                ON CONFLICT (request_hash) DO UPDATE SET
                    status = EXCLUDED.status, figi = EXCLUDED.figi, ticker = EXCLUDED.ticker,
                    retrieved_at = EXCLUDED.retrieved_at, expires_at = EXCLUDED.expires_at,
                    response_hash = EXCLUDED.response_hash
                """
            ),
            {
                "hash": item.get("request_hash"),
                "id_type": job.get("idType"),
                "id_value": job.get("idValue"),
                "exch": job.get("exchCode"),
                "ccy": job.get("currency"),
                "msd": job.get("marketSecDes"),
                "status": status,
                "figi": (item.get("chosen") or {}).get("figi"),
                "comp": (item.get("chosen") or {}).get("compositeFIGI"),
                "share": (item.get("chosen") or {}).get("shareClassFIGI"),
                "ticker": (item.get("chosen") or {}).get("ticker"),
                "name": (item.get("chosen") or {}).get("name"),
                "stype": (item.get("chosen") or {}).get("securityType"),
                "catalog": CATALOG_VERSION,
                "retrieved": retrieved,
                "expires": item.get("expires_at") or None,
                "resp": item.get("response_hash"),
            },
        )
        conn.execute(text("DELETE FROM mi_openfigi_candidates WHERE request_hash = :hash"), {"hash": item.get("request_hash")})
        if status == "AMBIGUOUS":
            for index, candidate in enumerate(item.get("candidates") or []):
                conn.execute(
                    text(
                        """
                        INSERT INTO mi_openfigi_candidates (request_hash, candidate_index, figi, ticker, exch_code, security_type, name, payload_json)
                        VALUES (:hash, :idx, :figi, :ticker, :exch, :stype, :name, CAST(:payload AS JSONB))
                        """
                    ),
                    {
                        "hash": item.get("request_hash"),
                        "idx": index,
                        "figi": candidate.get("figi"),
                        "ticker": candidate.get("ticker"),
                        "exch": candidate.get("exchCode"),
                        "stype": candidate.get("securityType"),
                        "name": candidate.get("name"),
                        "payload": json.dumps(candidate),
                    },
                )
    return counts
