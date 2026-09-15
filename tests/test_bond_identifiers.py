"""Bond identifier normalization and quote classification."""

from ibkr_collector.bond_identifiers import (
    QUOTE_STATUS_NOT_FOUND,
    QUOTE_STATUS_RESOLVED_AVAILABLE,
    QUOTE_STATUS_RESOLVED_ENTITLEMENT,
    QUOTE_STATUS_RESOLVED_NO_QUOTE,
    STORAGE_RIGHTS_PENDING,
    classify_bond_quote_outcome,
    ibkr_bond_contract_specs,
    normalize_cusip,
    normalize_isin,
    parse_bond_identifier,
)
import pytest


def test_normalize_cusip_and_isin():
    assert normalize_cusip("037833ey2") == "037833EY2"
    assert normalize_isin("us037833ey27") == "US037833EY27"
    with pytest.raises(ValueError):
        normalize_cusip("bad")
    with pytest.raises(ValueError):
        normalize_isin("US123")


def test_parse_and_contract_specs_prefer_symbol_cusip():
    ident = parse_bond_identifier(asset_class="corporate", cusip="037833EY2", isin="US037833EY27", label="AAPL 4 2028")
    specs = ibkr_bond_contract_specs(ident)
    assert specs[0]["method"] == "symbol=CUSIP"
    assert specs[0]["symbol"] == "037833EY2"
    assert any(s["method"] == "secIdType=ISIN" for s in specs)


def test_quote_classification_matrix():
    assert classify_bond_quote_outcome(con_id=None, ticks={}, error_codes=[]) == QUOTE_STATUS_NOT_FOUND
    assert (
        classify_bond_quote_outcome(con_id=1, ticks={"bid": 99.5}, error_codes=[10167])
        == QUOTE_STATUS_RESOLVED_AVAILABLE
    )
    assert (
        classify_bond_quote_outcome(con_id=1, ticks={}, error_codes=[354])
        == QUOTE_STATUS_RESOLVED_ENTITLEMENT
    )
    assert (
        classify_bond_quote_outcome(con_id=1, ticks={}, error_codes=[10167])
        == QUOTE_STATUS_RESOLVED_NO_QUOTE
    )
    assert STORAGE_RIGHTS_PENDING == "RIGHTS_PENDING"


def test_qs_failed_caption_is_not_masked():
    from market_intelligence.quote_status import exception_note

    note = exception_note(
        {
            "source_id": "QS_RESEARCH_DELIVERY",
            "access_status": "ON_DEMAND",
            "transport_status": "FAILED",
            "freshness_status": "FAILED",
            "policy_status": "FAILED",
        }
    )
    assert note.startswith("FAILED.")
    assert "last-known-good" not in note.lower() or "no usable" in note.lower()


def test_corporate_bond_remapped_rights_pending_is_not_opra_caption():
    from market_intelligence.quote_status import exception_note

    note = exception_note(
        {
            "source_id": "IBKR_CORPORATE_BONDS",
            "access_status": "ENTITLEMENT_REQUIRED",
            "policy_status": "RIGHTS_PENDING",
            "optional_disabled": True,
            "freshness_status": "UNKNOWN",
        }
    )
    assert "OPRA" not in note
    assert "CUSIP" in note
    assert "FINRA" in note
    assert "RIGHTS_PENDING" in note


def test_muni_bond_configuration_caption():
    from market_intelligence.quote_status import exception_note

    note = exception_note(
        {
            "source_id": "IBKR_MUNICIPAL_BONDS",
            "access_status": "CONFIGURATION_REQUIRED",
            "policy_status": "CONFIGURATION_REQUIRED",
            "optional_disabled": True,
        }
    )
    assert "MSRB" in note or "CUSIP" in note
    assert "OPRA" not in note


def test_corporate_adapter_reflects_cusip_resolution_proof():
    from market_intelligence.adapters import IBKRCorporateBondsAdapter

    status = IBKRCorporateBondsAdapter().probe({})
    assert status.access_status == "ENTITLEMENT_REQUIRED"
    assert "CUSIP" in status.reason
    assert status.enabled is False
    assert status.capabilities.get("storage") == "RIGHTS_PENDING"
