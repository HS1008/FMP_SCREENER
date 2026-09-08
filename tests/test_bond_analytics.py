"""Bond analytics (bounded domain) and disabled external adapters.

Pure-math tests run everywhere; the stored-bond batch test needs FMP_TEST_DATABASE_URL.
"""

from __future__ import annotations

import io
import json
import urllib.request
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from market_intelligence import adapters, bonds
from market_intelligence.bonds import BondAnalyticsError, BondTerms

FLAT_4 = {"DGS1": 4.0, "DGS2": 4.0, "DGS5": 4.0, "DGS10": 4.0, "DGS30": 4.0}


def _fixed(coupon=5.0, freq=2, maturity=date(2030, 1, 15), **kw) -> BondTerms:
    return BondTerms(bond_id="TEST", coupon_rate_pct=coupon, coupon_frequency=freq, maturity_date=maturity, settlement_days=0, **kw)


# ---- pure math ---------------------------------------------------------------------------------------


def test_par_bond_on_coupon_date_yields_its_coupon_and_roundtrips():
    terms = _fixed()
    settle = date(2025, 1, 15)
    assert bonds.price_from_yield(terms, settle, 5.0) == pytest.approx(100.0, abs=1e-9)
    assert bonds.yield_from_price(terms, settle, 100.0) == pytest.approx(5.0, abs=1e-8)
    for y in (0.5, 3.0, 7.25, 12.0):
        p = bonds.price_from_yield(terms, settle, y)
        assert bonds.yield_from_price(terms, settle, p) == pytest.approx(y, abs=1e-8)
    # Price/yield inverse relation.
    assert bonds.price_from_yield(terms, settle, 6.0) < 100.0 < bonds.price_from_yield(terms, settle, 4.0)


def test_duration_matches_closed_form_for_par_bond():
    terms = _fixed()
    settle = date(2025, 1, 15)
    mac, mod, conv = bonds.durations(terms, settle, 5.0, 100.0)
    # Closed form for a par bond: Macaulay = (1+y)/y * (1 - 1/(1+y)^n) in periods, y=2.5%, n=10.
    y, n = 0.025, 10
    expected_periods = (1 + y) / y * (1 - (1 + y) ** (-n))
    assert mac == pytest.approx(expected_periods / 2, rel=1e-9)
    assert mod == pytest.approx(mac / (1 + y), rel=1e-12)
    assert conv > 0
    # Modified duration approximates dP/dy: bump 1bp.
    p_up = bonds.price_from_yield(terms, settle, 5.01)
    p_dn = bonds.price_from_yield(terms, settle, 4.99)
    numeric_mod = -(p_up - p_dn) / (2 * 0.0001) / 100.0
    assert numeric_mod == pytest.approx(mod, rel=1e-4)


def test_accrued_interest_30_360_and_act_act():
    terms = _fixed(day_count="30/360")
    # Coupon 2.5 per period; 3 months of a 6-month period = 90/180.
    assert bonds.accrued_interest(terms, date(2025, 4, 15)) == pytest.approx(1.25)
    assert bonds.accrued_interest(terms, date(2025, 1, 15)) == 0.0
    aa = _fixed(day_count="ACT/ACT")
    # Jan 15 -> Apr 15 = 90 actual days over 181 (Jan 15 -> Jul 15).
    assert bonds.accrued_interest(aa, date(2025, 4, 15)) == pytest.approx(2.5 * 90 / 181)
    # Dirty = clean + accrued and the between-coupon yield stays continuous around par.
    mid = bonds.analyze_bond(terms, as_of=date(2025, 4, 15), clean_price=100.0)
    assert mid.dirty_price == pytest.approx(101.25)
    assert mid.ytm == pytest.approx(5.0, abs=0.05)


def test_zero_coupon_bond_has_no_accrued_and_ytm_from_discount():
    zero = BondTerms(bond_id="Z", coupon_rate_pct=0.0, coupon_frequency=2, maturity_date=date(2030, 1, 15), coupon_type="ZERO", settlement_days=0)
    result = bonds.analyze_bond(zero, as_of=date(2025, 1, 15), clean_price=78.35)
    assert result.accrued_interest == 0.0
    t = (date(2030, 1, 15) - date(2025, 1, 15)).days / 365.0
    expected = 2 * ((100 / 78.35) ** (1 / (2 * t)) - 1) * 100
    assert result.ytm == pytest.approx(expected, abs=1e-6)
    assert result.macaulay_duration == pytest.approx(t, rel=1e-9)
    assert result.support_status == bonds.SUPPORT_FULL


def test_flat_curve_gives_flat_zeros_and_zero_spread_for_curve_priced_bond():
    zeros = bonds.bootstrap_zero_curve(FLAT_4, max_years=10)
    assert len(zeros) == 20
    assert all(z == pytest.approx(4.0, abs=1e-9) for _, z in zeros)
    terms = _fixed()
    settle = date(2025, 1, 15)
    clean = bonds.price_from_yield(terms, settle, 4.0) - bonds.accrued_interest(terms, settle)
    result = bonds.analyze_bond(terms, as_of=date(2025, 1, 15), clean_price=clean, treasury_curve=FLAT_4)
    assert result.g_spread_bps == pytest.approx(0.0, abs=1e-6)
    assert result.z_spread_bps == pytest.approx(0.0, abs=1e-6)
    par = bonds.analyze_bond(terms, as_of=date(2025, 1, 15), clean_price=100.0, treasury_curve=FLAT_4)
    assert par.g_spread_bps == pytest.approx(100.0, abs=1e-6)
    assert par.z_spread_bps == pytest.approx(100.0, abs=0.5)
    assert par.oas_bps is None
    assert par.as_row()["detail_json"]["oas_status"] == bonds.OAS_STATUS


def test_g_spread_requires_curve_coverage_and_interpolates_linearly():
    assert bonds.interpolate_par_yield({"DGS2": 4.0, "DGS10": 5.0}, 6.0) == pytest.approx(4.5)
    assert bonds.interpolate_par_yield({"DGS2": 4.0, "DGS10": 5.0}, 1.0) is None
    assert bonds.interpolate_par_yield({"DGS2": 4.0, "DGS10": 5.0}, 12.0) is None
    assert bonds.interpolate_par_yield({"DGS2": 4.0}, 2.0) is None
    long_bond = _fixed(maturity=date(2050, 1, 15))
    result = bonds.analyze_bond(long_bond, as_of=date(2025, 1, 15), clean_price=100.0, treasury_curve={"DGS2": 4.0, "DGS10": 5.0})
    assert result.g_spread_bps is None
    assert result.support_status == bonds.SUPPORT_FULL
    no_curve = bonds.analyze_bond(_fixed(), as_of=date(2025, 1, 15), clean_price=100.0)
    assert no_curve.g_spread_bps is None and no_curve.z_spread_bps is None
    assert no_curve.detail["spreads"] == "no treasury curve supplied"


def test_unsupported_structures_are_flagged_not_faked():
    callable_bond = _fixed(callable=True)
    result = bonds.analyze_bond(callable_bond, as_of=date(2025, 1, 15), clean_price=100.0, treasury_curve=FLAT_4)
    assert result.support_status == bonds.SUPPORT_YTM_ONLY_CALLABLE
    assert result.ytm == pytest.approx(5.0, abs=1e-8)
    assert result.modified_duration is None and result.g_spread_bps is None and result.z_spread_bps is None
    floater = BondTerms(bond_id="F", coupon_rate_pct=None, coupon_frequency=4, maturity_date=date(2030, 1, 15), coupon_type="FLOATING", settlement_days=0)
    result = bonds.analyze_bond(floater, as_of=date(2025, 1, 15), clean_price=99.0)
    assert result.support_status == bonds.SUPPORT_UNSUPPORTED_FLOATING
    assert result.ytm is None and result.dirty_price is None
    with pytest.raises(BondAnalyticsError):
        _fixed(freq=3).validate()
    with pytest.raises(BondAnalyticsError):
        _fixed(day_count="ACT/ACT/ISDA").validate()
    with pytest.raises(BondAnalyticsError):
        BondTerms(bond_id="X", coupon_rate_pct=None, coupon_frequency=2, maturity_date=date(2030, 1, 1)).validate()
    with pytest.raises(BondAnalyticsError, match="maturity"):
        bonds.analyze_bond(_fixed(), as_of=date(2030, 1, 15), clean_price=100.0)


def test_terms_from_row_refuses_unverified_or_incomplete_terms():
    base = {"bond_id": "B1", "coupon_rate": "5", "coupon_frequency": 2, "maturity_date": date(2030, 1, 15), "terms_status": "SOURCE_PROVIDED", "coupon_type": "FIXED", "day_count": "30/360"}
    assert bonds.terms_from_row(base).coupon_rate_pct == 5.0
    with pytest.raises(BondAnalyticsError, match="not source-verified"):
        bonds.terms_from_row(dict(base, terms_status="UNVERIFIED"))
    with pytest.raises(BondAnalyticsError, match="incomplete"):
        bonds.terms_from_row(dict(base, maturity_date=None))


def test_settlement_skips_weekends():
    assert bonds.settlement_from(date(2025, 1, 17), 1) == date(2025, 1, 20)  # Fri -> Mon
    assert bonds.settlement_from(date(2025, 1, 15), 2) == date(2025, 1, 17)
    assert bonds.settlement_from(date(2025, 1, 15), 0) == date(2025, 1, 15)


# ---- external adapters ------------------------------------------------------------------------------


def test_adapters_are_disabled_or_configuration_required_and_refuse_to_fetch():
    statuses = adapters.probe_all({})
    assert set(statuses) == {"IBKR_MARKET_DATA", "FINRA_TRACE", "SEC_EDGAR"}
    assert statuses["IBKR_MARKET_DATA"].access_status == adapters.ACCESS_DISABLED
    assert statuses["FINRA_TRACE"].access_status == adapters.ACCESS_DISABLED
    assert statuses["SEC_EDGAR"].access_status == adapters.ACCESS_CONFIGURATION_REQUIRED
    assert not any(s.enabled for s in statuses.values())
    with pytest.raises(adapters.AdapterDisabled):
        adapters.IBKRMarketDataAdapter().fetch_quotes(["TLT"], env={"MI_IBKR_MARKET_DATA_ENABLED": "1"})
    assert not any(name.lower().startswith(("place", "order", "submit")) for name in dir(adapters.IBKRMarketDataAdapter))
    trace = adapters.TraceAdapter()
    assert trace.probe({"MI_TRACE_ENABLED": "1"}).access_status == adapters.ACCESS_CONFIGURATION_REQUIRED
    assert trace.probe({"MI_TRACE_ENABLED": "1", "FINRA_API_CLIENT_ID": "x", "FINRA_API_CLIENT_SECRET": "y"}).access_status == adapters.ACCESS_ENTITLEMENT_REQUIRED
    with pytest.raises(adapters.AdapterDisabled):
        trace.fetch_trades(["912828ZT0"], env={"MI_TRACE_ENABLED": "1", "FINRA_API_CLIENT_ID": "x", "FINRA_API_CLIENT_SECRET": "y"})


def test_edgar_requires_contact_user_agent_and_opt_in_and_never_calls_when_disabled():
    calls: list[urllib.request.Request] = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout):
        calls.append(request)
        return _Resp(json.dumps({"cik": 1318605, "name": "TEST CO"}).encode())

    edgar = adapters.EdgarAdapter(opener=opener, min_interval_s=0)
    assert edgar.probe({}).access_status == adapters.ACCESS_CONFIGURATION_REQUIRED
    assert edgar.probe({"SEC_USER_AGENT": "NoContactHere"}).access_status == adapters.ACCESS_CONFIGURATION_REQUIRED
    assert edgar.probe({"SEC_USER_AGENT": "FMP Research ops@example.com"}).access_status == adapters.ACCESS_DISABLED
    with pytest.raises(adapters.AdapterDisabled):
        edgar.submissions("1318605", env={"SEC_USER_AGENT": "FMP Research ops@example.com"})
    assert calls == []
    env = {"SEC_USER_AGENT": "FMP Research ops@example.com", "MI_EDGAR_ENABLED": "1"}
    assert edgar.probe(env).enabled is True
    payload = edgar.submissions("1318605", env=env)
    assert payload["name"] == "TEST CO"
    assert calls[0].full_url == "https://data.sec.gov/submissions/CIK0001318605.json"
    assert calls[0].get_header("User-agent") == "FMP Research ops@example.com"
    with pytest.raises(ValueError):
        adapters.EdgarAdapter.normalize_cik("not-a-cik")


def test_refresh_plan_reports_external_adapter_status_without_db(capsys):
    from jobs.market_intelligence_refresh import run

    code = run(["--all-configured", "--dry-run", "--json"], env={"HOME": "/tmp"})
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    external = payload["plan"]["external_adapters"]
    assert external["IBKR_MARKET_DATA"]["access_status"] == "DISABLED"
    assert external["SEC_EDGAR"]["access_status"] == "CONFIGURATION_REQUIRED"
    assert not any(v["enabled"] for v in external.values())


# ---- stored bonds (PostgreSQL) -------------------------------------------------------------------------


def _seed_curve(conn, as_of: date) -> None:
    from market_intelligence import store

    store.upsert_source_registry(conn)
    for sid, value in FLAT_4.items():
        store.upsert_macro_series(
            conn,
            series_id=sid,
            source_id="FRED",
            provider_series_id=sid,
            spec_fields={"category": "rates", "subcategory": "nominal_curve", "catalog_version": "fred_catalog_v1", "source_url": "https://fred.stlouisfed.org/series/{0}".format(sid), "notes": "", "export_scope": "ATTRIBUTION_REQUIRED", "expected_frequency": "D"},
            meta={"title": sid, "units": "Percent", "frequency_short": "D", "seasonal_adjustment_short": "NSA"},
            metadata_status="VALIDATED",
            mismatches=None,
        )
        store.upsert_observations(conn, series_id=sid, rows=[store.ObservationInput(as_of, str(value))], retrieved_at=datetime(2025, 1, 15, 22, tzinfo=timezone.utc), run_id=None)


def _seed_bond(conn, bond_id: str, *, terms_status: str = "SOURCE_PROVIDED", callable_flag: bool = False, quote: tuple[float, float] | None = (99.5, 100.5)) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mi_bond_securities (bond_id, issuer_name, currency, coupon_type, coupon_rate, coupon_frequency, issue_date, maturity_date,
                day_count, settlement_days, redemption, callable, putable, source_id, terms_status)
            VALUES (:bond_id, 'Test Issuer', 'USD', 'FIXED', 5.0, 2, '2020-01-15', '2030-01-15', '30/360', 1, 100, :callable, FALSE, 'SEC_EDGAR', :terms_status)
            """
        ),
        {"bond_id": bond_id, "callable": callable_flag, "terms_status": terms_status},
    )
    if quote is not None:
        conn.execute(
            text(
                """
                INSERT INTO mi_bond_quotes (bond_id, source_id, quote_ts, price_kind, bid_price, ask_price, price_per, delay_status, entitlement_status, retrieved_at)
                VALUES (:bond_id, 'TEST_QUOTES', '2025-01-14T21:00:00+00:00', 'MID', :bid, :ask, '100', 'DELAYED', 'TEST', NOW())
                """
            ),
            {"bond_id": bond_id, "bid": quote[0], "ask": quote[1]},
        )


def test_stored_bond_batch_uses_fred_curve_and_reports_skips(mi_db):
    as_of = date(2025, 1, 14)
    with mi_db.begin() as conn:
        _seed_curve(conn, as_of)
        _seed_bond(conn, "OK_BULLET")
        _seed_bond(conn, "CALLABLE", callable_flag=True)
        _seed_bond(conn, "UNVERIFIED", terms_status="UNVERIFIED")
        _seed_bond(conn, "NO_QUOTE", quote=None)
        report = bonds.compute_stored_bond_analytics(conn, as_of=as_of)
    assert report.curve_date == as_of and report.curve_points == len(FLAT_4)
    assert report.computed == 2
    assert report.support == {bonds.SUPPORT_FULL: 1, bonds.SUPPORT_YTM_ONLY_CALLABLE: 1}
    skipped = {s["bond_id"]: s["reason"] for s in report.skipped}
    assert "not source-verified" in skipped["UNVERIFIED"]
    assert "no quote" in skipped["NO_QUOTE"]
    with mi_db.connect() as conn:
        rows = conn.execute(text("SELECT bond_id, settlement_date, clean_price, ytm, g_spread_bps, z_spread_bps, oas_bps, support_status, detail_json FROM mi_bond_analytics ORDER BY bond_id")).mappings().all()
    by_id = {r["bond_id"]: r for r in rows}
    assert set(by_id) == {"OK_BULLET", "CALLABLE"}
    ok = by_id["OK_BULLET"]
    assert ok["settlement_date"] == date(2025, 1, 15)
    assert float(ok["clean_price"]) == pytest.approx(100.0)
    assert float(ok["ytm"]) == pytest.approx(5.0, abs=1e-6)
    assert float(ok["g_spread_bps"]) == pytest.approx(100.0, abs=1e-4)
    assert ok["oas_bps"] is None
    assert ok["detail_json"]["oas_status"] == bonds.OAS_STATUS
    assert ok["detail_json"]["curve_date"] == "2025-01-14"
    assert by_id["CALLABLE"]["g_spread_bps"] is None
    # Idempotent re-run: same rows, no duplicates.
    with mi_db.begin() as conn:
        again = bonds.compute_stored_bond_analytics(conn, as_of=as_of)
    with mi_db.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM mi_bond_analytics")).scalar()
    assert again.computed == 2 and count == 2


def test_bond_analytics_job_dry_run_writes_nothing(mi_db, capsys):
    from jobs.bond_analytics import run

    with mi_db.begin() as conn:
        _seed_curve(conn, date(2025, 1, 14))
        _seed_bond(conn, "OK_BULLET")
    assert run(["--as-of", "2025-01-14", "--dry-run", "--json"], engine=mi_db) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "DRY_RUN" and payload["computed"] == 1
    with mi_db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM mi_bond_analytics")).scalar() == 0
    assert run(["--as-of", "2025-01-14", "--json"], engine=mi_db) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "SUCCEEDED"
    with mi_db.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM mi_bond_analytics")).scalar() == 1
        run_row = conn.execute(text("SELECT status, dataset FROM mi_ingestion_runs WHERE source_id = 'ANALYTICS' AND dataset = 'bond_analytics'")).first()
    assert run_row is not None and run_row[0] == "SUCCEEDED"
