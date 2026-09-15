"""Bond identifier normalization and IBKR contract construction. No network I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

AssetClass = Literal["corporate", "municipal", "treasury", "other"]

_CUSIP_RE = re.compile(r"^[0-9A-Z]{9}$")
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


@dataclass(frozen=True)
class BondIdentifier:
    asset_class: AssetClass
    cusip: str | None = None
    isin: str | None = None
    con_id: int | None = None
    label: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_class": self.asset_class,
            "cusip": self.cusip,
            "isin": self.isin,
            "con_id": self.con_id,
            "label": self.label,
        }


def normalize_cusip(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip().upper().replace(" ", "").replace("-", "")
    if not text:
        return None
    if not _CUSIP_RE.match(text):
        raise ValueError("invalid CUSIP {0!r}".format(raw))
    return text


def normalize_isin(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip().upper().replace(" ", "").replace("-", "")
    if not text:
        return None
    if not _ISIN_RE.match(text):
        raise ValueError("invalid ISIN {0!r}".format(raw))
    return text


def parse_bond_identifier(
    *,
    asset_class: AssetClass,
    cusip: str | None = None,
    isin: str | None = None,
    con_id: int | None = None,
    label: str | None = None,
) -> BondIdentifier:
    norm_cusip = normalize_cusip(cusip)
    norm_isin = normalize_isin(isin)
    cid = int(con_id) if con_id not in (None, "", 0, "0") else None
    if cid is not None and cid <= 0:
        raise ValueError("con_id must be positive")
    if norm_cusip is None and norm_isin is None and cid is None:
        raise ValueError("provide cusip, isin, and/or con_id")
    return BondIdentifier(asset_class=asset_class, cusip=norm_cusip, isin=norm_isin, con_id=cid, label=label)


def ibkr_bond_contract_specs(identifier: BondIdentifier) -> list[dict[str, Any]]:
    """Ordered IBKR Contract field attempts. Prefer symbol=CUSIP (proven live)."""
    specs: list[dict[str, Any]] = []
    if identifier.con_id:
        specs.append({"method": "conId", "conId": identifier.con_id, "secType": "BOND", "currency": "USD", "exchange": "SMART"})
    if identifier.cusip:
        specs.append(
            {
                "method": "symbol=CUSIP",
                "symbol": identifier.cusip,
                "secType": "BOND",
                "currency": "USD",
                "exchange": "SMART",
            }
        )
        specs.append(
            {
                "method": "secIdType=CUSIP",
                "secIdType": "CUSIP",
                "secId": identifier.cusip,
                "secType": "BOND",
                "currency": "USD",
                "exchange": "SMART",
            }
        )
    if identifier.isin:
        specs.append(
            {
                "method": "secIdType=ISIN",
                "secIdType": "ISIN",
                "secId": identifier.isin,
                "secType": "BOND",
                "currency": "USD",
                "exchange": "SMART",
            }
        )
    return specs


QUOTE_STATUS_RESOLVED_AVAILABLE = "IDENTIFIER_RESOLVED_QUOTE_AVAILABLE"
QUOTE_STATUS_RESOLVED_ENTITLEMENT = "IDENTIFIER_RESOLVED_ENTITLEMENT_REQUIRED"
QUOTE_STATUS_RESOLVED_NO_QUOTE = "IDENTIFIER_RESOLVED_NO_QUOTE"
QUOTE_STATUS_NOT_FOUND = "CONTRACT_NOT_FOUND"
QUOTE_STATUS_PROVIDER = "PROVIDER_SUPPORT_REQUIRED"
STORAGE_RIGHTS_PENDING = "RIGHTS_PENDING"

HARD_ENTITLEMENT_CODES = frozenset({354, 10089, 10091, 10168, 10197, 10225, 2186})
DELAYED_NOTICE_CODES = frozenset({10167})


def classify_bond_quote_outcome(
    *,
    con_id: int | None,
    ticks: dict[str, Any] | None,
    error_codes: list[int] | None,
) -> str:
    ticks = ticks or {}
    codes = {int(c) for c in (error_codes or [])}
    if not con_id:
        return QUOTE_STATUS_NOT_FOUND
    has_quote = any(ticks.get(k) is not None for k in ("bid", "ask", "last", "close"))
    if has_quote:
        return QUOTE_STATUS_RESOLVED_AVAILABLE
    if codes & HARD_ENTITLEMENT_CODES:
        return QUOTE_STATUS_RESOLVED_ENTITLEMENT
    # 10167 alone is delayed-farm notice, not proof of entitlement denial.
    if codes & DELAYED_NOTICE_CODES and not has_quote:
        return QUOTE_STATUS_RESOLVED_NO_QUOTE
    return QUOTE_STATUS_RESOLVED_NO_QUOTE
