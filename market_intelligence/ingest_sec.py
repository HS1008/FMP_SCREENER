"""Bounded SEC EDGAR ingest into canonical filing/event tables."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from sqlalchemy import text

from market_intelligence.adapters import EdgarAdapter
from market_intelligence.sec_events import event_from_form
from market_intelligence.sec_metrics import fcf
from market_intelligence.store import RUN_FAILED, RUN_SUCCEEDED, finish_run, start_run

DEFAULT_CIKS = ("320193",)  # AAPL as a representative ordinary issuer seed
BOUNDED_CONCEPTS = (
    "Revenues",
    "NetIncomeLoss",
    "Assets",
    "Liabilities",
    "StockholdersEquity",
    "NetCashProvidedByUsedInOperatingActivities",
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "EarningsPerShareDiluted",
    "CommonStockSharesOutstanding",
)
FACT_LIMIT_PER_CIK = 12


def extract_company_facts(payload: Mapping[str, Any], *, cik: str, limit: int = FACT_LIMIT_PER_CIK) -> list[dict[str, Any]]:
    """Take a bounded set of standard-taxonomy facts. Do not invent frames or custom concepts."""
    facts = payload.get("facts") or {}
    out: list[dict[str, Any]] = []
    for taxonomy in ("us-gaap", "dei"):
        concepts = facts.get(taxonomy) or {}
        if not isinstance(concepts, dict):
            continue
        for concept in BOUNDED_CONCEPTS:
            body = concepts.get(concept) or {}
            units = body.get("units") if isinstance(body, dict) else None
            if not isinstance(units, dict):
                continue
            for unit_name, rows in units.items():
                if not isinstance(rows, list) or not rows:
                    continue
                for item in rows[-1:]:
                    if not isinstance(item, dict):
                        continue
                    start = item.get("start")
                    end = item.get("end")
                    instant = None if start or end else item.get("filed")
                    duration = "DURATION" if start or end else "INSTANT"
                    raw = json.dumps(
                        {
                            "cik": cik,
                            "taxonomy": taxonomy,
                            "concept": concept,
                            "unit": unit_name,
                            "start": start,
                            "end": end,
                            "val": item.get("val"),
                            "accn": item.get("accn"),
                            "frame": item.get("frame"),
                        },
                        sort_keys=True,
                        default=str,
                    )
                    out.append(
                        {
                            "cik": cik,
                            "accession": str(item.get("accn") or "").replace("-", "") or None,
                            "taxonomy": taxonomy,
                            "concept": concept,
                            "unit": str(unit_name)[:32],
                            "currency": "USD" if str(unit_name).upper() in {"USD", "USD/SHARES"} else None,
                            "period_start": start,
                            "period_end": end,
                            "instant_date": instant,
                            "duration_or_instant": duration,
                            "fiscal_year": item.get("fy"),
                            "fiscal_period": item.get("fp"),
                            "value": item.get("val"),
                            "frame": item.get("frame"),
                            "original_or_amended": "AMENDED" if str(item.get("form") or "").upper().endswith("/A") else "ORIGINAL",
                            "payload_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                        }
                    )
                    if len(out) >= limit:
                        return out
    return out


def ingest_sec(engine, adapter: EdgarAdapter, *, env: Mapping[str, str], parent_run_id: str | None = None, ciks: tuple[str, ...] | None = None) -> dict[str, Any]:
    configured = tuple(item.strip() for item in str(env.get("MI_EDGAR_CIKS") or "").split(",") if item.strip())
    ciks = ciks or configured or DEFAULT_CIKS
    report: dict[str, Any] = {"source_id": adapter.source_id, "failed": False, "ciks": []}
    with engine.begin() as conn:
        run_id = start_run(conn, source_id=adapter.source_id, dataset="sec_filings_facts", parent_run_id=parent_run_id)
        try:
            for cik in ciks:
                submissions = adapter.submissions(cik, env=env)
                recent = (submissions.get("filings") or {}).get("recent") or {}
                accessions = list(recent.get("accessionNumber") or [])[:5]
                forms = list(recent.get("form") or [])
                filed = list(recent.get("filingDate") or [])
                for index, accession in enumerate(accessions):
                    form = forms[index] if index < len(forms) else ""
                    filed_at = filed[index] if index < len(filed) else None
                    compact = accession.replace("-", "")
                    event = event_from_form(form)
                    conn.execute(
                        text(
                            """
                            INSERT INTO mi_sec_filings (
                                accession, cik, form, filed_at, available_at, available_at_basis,
                                retrieved_at, ingestion_run_id, content_sha256, source_url, is_amendment
                            ) VALUES (
                                :acc, :cik, :form, CAST(:filed AS DATE), NOW(), 'ACCEPTANCE', NOW(), :run_id, :sha,
                                :url, :amend
                            )
                            ON CONFLICT (accession) DO NOTHING
                            """
                        ),
                        {
                            "acc": compact,
                            "cik": adapter.normalize_cik(cik),
                            "form": form,
                            "filed": filed_at,
                            "run_id": run_id,
                            "sha": hashlib.sha256(accession.encode("utf-8")).hexdigest(),
                            "url": "https://www.sec.gov/Archives/edgar/data/{0}/{1}/".format(int(adapter.normalize_cik(cik)), compact),
                            "amend": str(form).upper().endswith("/A"),
                        },
                    )
                    if event.get("status") == "OK":
                        dedupe = "{0}:{1}:{2}".format(adapter.normalize_cik(cik), compact, event["event_type"])
                        conn.execute(
                            text(
                                """
                                INSERT INTO mi_corporate_events (
                                    event_id, cik, event_type, disclosed_at, importance, confidence,
                                    accession, source_url, summary, dedupe_key
                                ) VALUES (
                                    :eid, :cik, :etype, CAST(:filed AS DATE), 'ROUTINE', :conf, :acc, :url, :summary, :dedupe
                                )
                                ON CONFLICT (event_id) DO NOTHING
                                """
                            ),
                            {
                                "eid": hashlib.sha256(dedupe.encode("utf-8")).hexdigest()[:32],
                                "cik": adapter.normalize_cik(cik),
                                "etype": event["event_type"],
                                "filed": filed_at,
                                "conf": event["confidence"],
                                "acc": compact,
                                "url": "https://www.sec.gov/Archives/edgar/data/{0}/{1}/".format(int(adapter.normalize_cik(cik)), compact),
                                "summary": "form {0}".format(form),
                                "dedupe": dedupe,
                            },
                        )
                facts_n = 0
                try:
                    payload = adapter.company_facts(cik, env=env)
                    extracted = extract_company_facts(payload, cik=adapter.normalize_cik(cik))
                    for fact in extracted:
                        conn.execute(
                            text(
                                """
                                INSERT INTO mi_financial_facts (
                                    cik, accession, taxonomy, concept, unit, currency, period_start, period_end,
                                    instant_date, duration_or_instant, fiscal_year, fiscal_period, value, frame,
                                    original_or_amended, available_at, available_at_basis, retrieved_at,
                                    ingestion_run_id, payload_hash
                                )
                                SELECT
                                    :cik, :acc, :tax, :concept, :unit, :ccy, CAST(:start AS DATE), CAST(:end AS DATE),
                                    CAST(:instant AS DATE), :doi, :fy, :fp, :value, :frame, :orig, NOW(), 'ACCEPTANCE',
                                    NOW(), :run_id, :hash
                                WHERE NOT EXISTS (SELECT 1 FROM mi_financial_facts WHERE payload_hash = :hash)
                                """
                            ),
                            {
                                "cik": fact["cik"],
                                "acc": fact["accession"],
                                "tax": fact["taxonomy"],
                                "concept": fact["concept"],
                                "unit": fact["unit"],
                                "ccy": fact["currency"],
                                "start": fact["period_start"],
                                "end": fact["period_end"],
                                "instant": fact["instant_date"],
                                "doi": fact["duration_or_instant"],
                                "fy": fact["fiscal_year"],
                                "fp": fact["fiscal_period"],
                                "value": fact["value"],
                                "frame": fact["frame"],
                                "orig": fact["original_or_amended"],
                                "run_id": run_id,
                                "hash": fact["payload_hash"],
                            },
                        )
                        facts_n += 1
                    by_concept = {row["concept"]: row["value"] for row in extracted}
                    cash = fcf(
                        by_concept.get("NetCashProvidedByUsedInOperatingActivities"),
                        by_concept.get("PaymentsToAcquirePropertyPlantAndEquipment"),
                    )
                    as_of = next((row["period_end"] for row in extracted if row.get("period_end")), None)
                    if as_of:
                        conn.execute(
                            text(
                                """
                                INSERT INTO mi_financial_metrics (
                                    metric_id, cik, as_of, method_version, value, units, status, inputs_json, missing_prerequisites
                                ) VALUES (
                                    'FCF', :cik, CAST(:as_of AS DATE), 'sec_metrics_v1', :value, 'USD', :status, CAST(:inputs AS JSONB), :missing
                                )
                                ON CONFLICT (metric_id, cik, as_of, method_version) DO NOTHING
                                """
                            ),
                            {
                                "cik": adapter.normalize_cik(cik),
                                "as_of": as_of,
                                "value": str(cash["value"]) if cash.get("value") is not None else None,
                                "status": cash["status"],
                                "inputs": json.dumps({"inputs": {k: str(v) for k, v in (cash.get("inputs") or {}).items()}}, default=str),
                                "missing": cash.get("reason"),
                            },
                        )
                except Exception as facts_exc:  # noqa: BLE001
                    report.setdefault("facts_errors", []).append(facts_exc.__class__.__name__)
                report["ciks"].append({"cik": adapter.normalize_cik(cik), "filings": len(accessions), "facts": facts_n})
            finish_run(conn, run_id, status=RUN_SUCCEEDED)
        except Exception as exc:  # noqa: BLE001
            finish_run(conn, run_id, status=RUN_FAILED, error_redacted=exc.__class__.__name__)
            report["failed"] = True
            report["error"] = exc.__class__.__name__
    return report
