"""Official FINRA Query API datasets used for corporate-bond activity.

Dataset names, groups, and field identities come from the FINRA Developer Center
catalog (fixedIncomeMarket). TRACE individual-transaction APIs are a different
product (TRAQS / TRACE API) and are not part of the Query API platform.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FINRA_QUERY_SOURCE_ID = "FINRA_QUERY"
FINRA_TRACE_SOURCE_ID = "FINRA_TRACE"
FINRA_CATALOG_VERSION = "finra_query_v1"
FINRA_GROUP = "fixedIncomeMarket"
FINRA_API_BASE = "https://api.finra.org"
FINRA_TOKEN_URL = "https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token?grant_type=client_credentials"
FINRA_ATTRIBUTION = (
    "FINRA TRACE-derived aggregates via the FINRA Query API. Not a live order book. "
    "Not endorsed as TRACE tape access."
)
FINRA_TERMS = (
    "Internal display of Query API public fixed-income aggregates. Individual corporate-bond "
    "transaction datasets and licensed TRACE dissemination products are separate entitlements. "
    "Regulatory trade-reporting/submission APIs are out of scope."
)

CAP_AVAILABLE = "AVAILABLE"
CAP_CONFIGURATION_REQUIRED = "CONFIGURATION_REQUIRED"
CAP_ENTITLEMENT_REQUIRED = "ENTITLEMENT_REQUIRED"
CAP_TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
CAP_DISABLED = "DISABLED"
CAP_NEVER_ATTEMPTED = "NEVER_ATTEMPTED"

KIND_BREADTH = "breadth"
KIND_SENTIMENT = "sentiment"
KIND_CAPPED_VOLUME = "capped_volume"
KIND_INDIVIDUAL_TRADES = "individual_trades"

DATE_FIELD = "tradeReportDate"


@dataclass(frozen=True)
class FinraDatasetSpec:
    dataset: str
    group: str
    source_id: str
    title: str
    kind: str
    date_field: str
    category_fields: tuple[str, ...]
    grain_fields: tuple[str, ...]
    numeric_fields: tuple[str, ...]
    volume_is_capped: bool
    cadence: str
    coverage_note: str
    units_note: str
    query_api: bool
    backfill_calendar_days: int = 45
    overlap_days: int = 5
    probe_limit: int = 5
    mock_dataset: str | None = None
    extra_fields: tuple[str, ...] = field(default_factory=tuple)


CORPORATE_BREADTH = FinraDatasetSpec(
    dataset="corporateMarketBreadth",
    group=FINRA_GROUP,
    source_id=FINRA_QUERY_SOURCE_ID,
    title="Corporate Debt Market Breadth",
    kind=KIND_BREADTH,
    date_field=DATE_FIELD,
    category_fields=("productCategory",),
    grain_fields=("productCategory",),
    numeric_fields=(
        "totalVolume",
        "totalTrades",
        "advances",
        "declines",
        "unchanged",
        "fiftyTwoWeekHigh",
        "fiftyTwoWeekLow",
    ),
    volume_is_capped=False,
    cadence="D",
    coverage_note=(
        "Daily TRACE-derived market breadth for corporate debt, by FINRA productCategory "
        "(all securities, investment grade, high yield, convertibles). Aggregate counts, "
        "not individual trades. Categories overlap: do not add IG+HY+convertibles to "
        "reproduce all securities."
    ),
    units_note=(
        "Source fields as published by FINRA Query API corporateMarketBreadth "
        "(totalVolume, totalTrades, advances, declines, unchanged, 52-week high/low counts). "
        "Do not mix with ICE OAS (bps) or Treasury yields (%)."
    ),
    query_api=True,
    mock_dataset="corporateMarketBreadthMock",
)

CORPORATE_SENTIMENT = FinraDatasetSpec(
    dataset="corporateMarketSentiment",
    group=FINRA_GROUP,
    source_id=FINRA_QUERY_SOURCE_ID,
    title="Corporate Debt Market Sentiment",
    kind=KIND_SENTIMENT,
    date_field=DATE_FIELD,
    category_fields=("tradeType", "productCategory"),
    grain_fields=("tradeType", "productCategory"),
    numeric_fields=("totalVolume", "totalTrades", "totalTransactions"),
    volume_is_capped=False,
    cadence="D",
    coverage_note=(
        "Daily TRACE-derived sentiment rows. productCategory values include FINRA's "
        "customer buy, customer sell, affiliate buy, affiliate sell, and all securities. "
        "Customer buy/sell is the reporting dealer's contra-party classification, not "
        "buyer-initiated vs seller-initiated and not institutional identity."
    ),
    units_note=(
        "Source fields as published by FINRA Query API corporateMarketSentiment "
        "(totalVolume, totalTrades, totalTransactions). Signed customer net is computed "
        "only from the documented customer buy and customer sell rows for the same "
        "tradeType and tradeReportDate."
    ),
    query_api=True,
    mock_dataset="corporateMarketSentimentMock",
)

CORPORATE_CAPPED_VOLUME = FinraDatasetSpec(
    dataset="corporatesAndAgenciesCappedVolume",
    group=FINRA_GROUP,
    source_id=FINRA_QUERY_SOURCE_ID,
    title="Corporate And Agency Capped Volume",
    kind=KIND_CAPPED_VOLUME,
    date_field=DATE_FIELD,
    category_fields=("gradeCode", "144AFlag"),
    grain_fields=("gradeCode", "144AFlag"),
    numeric_fields=(
        "totalTradeCount",
        "totalVolumeQuantity",
        "customerBuyParLessThan5YearsQuantity",
        "customerSellParLessThan5YearsQuantity",
        "customerBuyParGreaterThan5YearsLessThan10YearsQuantity",
        "customerSellParGreaterThan5YearsLessThan10YearsQuantity",
        "customerBuyParGreaterThan10YearsLessThan25YearsQuantity",
        "customerSellParGreaterThan10YearsLessThan25YearsQuantity",
        "customerBuyParGreaterThan25YearsQuantity",
        "customerSellParGreaterThan25YearsQuantity",
        "customerBuyTradeMaturityLessThan5YearsCount",
        "customerSellTradeMaturityLessThan5YearsCount",
        "customerBuyTradeMaturityGreaterThan5YearsLessThan10YearsCount",
        "customerSellTradeMaturityGreaterThan5YearsLessThan10YearsCount",
        "customerBuyTradeMaturityGreaterThan10YearsLessThan25YearsCount",
        "customerSellTradeMaturityGreaterThan10YearsLessThan25YearsCount",
        "customerBuyTradeMaturityGreaterThan25YearsCount",
        "customerSellTradeMaturityGreaterThan25YearsCount",
        "interdealerParLessThan5yearsQuantity",
        "interdealerParGreaterThan5yearsLessThan10YearsQuantity",
        "parMaturityLessThan5YearsQuantity",
        "parMaturityGreaterThan5YearsLessThan10YearsQuantity",
        "parMaturityGreaterThan10YearsLessThan25YearsQuantity",
        "parMaturityGreaterThan25YearsQuantity",
        "parLessThan5MillionQuantity",
        "parGreaterThan5MillionLessThan10MillionQuantity",
        "parGreaterThan10MillionLessThan25MillionQuantity",
        "parGreaterThan25MillionQuantity",
        "tradeLessThan5MillionCount",
        "tradeGreaterThan5MillionLessThan10MillionCount",
        "tradeGreaterThan10MillionLessThan25MillionCount",
        "tradeGreaterThan25MillionCount",
        "tradeMaturityLessThan5YearsCount",
        "tradeMaturityGreaterThan5YearsLessThan10YearsCount",
        "tradeMaturityGreaterThan10YearsLessThan25YearsCount",
        "tradeMaturityGreaterThan25YearsCount",
        "tradeYear",
        "tradeMonth",
    ),
    volume_is_capped=True,
    cadence="M",
    coverage_note=(
        "Capped/reported TRACE volume for corporate and agency debt, split by FINRA "
        "gradeCode (IG, HY, AGCY) and 144AFlag. Live Query API rows use month-start "
        "tradeReportDate values; treat this dataset as monthly, not a partial session. "
        "Totals are lower-bound/capped measures where FINRA caps size. Do not describe "
        "size-weighted statistics as exact VWAP. Do not add IG+HY+AGCY 144A/non-144A "
        "rows as if they were a single tape."
    ),
    units_note=(
        "Source fields as published by FINRA Query API corporatesAndAgenciesCappedVolume. "
        "totalVolumeQuantity and par* fields are capped/reported source quantities, not "
        "uncapped par. Customer buy/sell par fields use the dealer-reported customer "
        "perspective."
    ),
    query_api=True,
    mock_dataset="corporatesAndAgenciesCappedVolumeMock",
)

TRACE_INDIVIDUAL = FinraDatasetSpec(
    dataset="TRACE_INDIVIDUAL_TRANSACTIONS",
    group="traceApi",
    source_id=FINRA_TRACE_SOURCE_ID,
    title="Individual TRACE corporate-bond transactions",
    kind=KIND_INDIVIDUAL_TRADES,
    date_field="trade_ts",
    category_fields=(),
    grain_fields=(),
    numeric_fields=(),
    volume_is_capped=False,
    cadence="INTRADAY",
    coverage_note=(
        "The FINRA Query API does not include individual TRACE corporate-bond prints. "
        "FINRA documents the TRACE API / TRAQS file-download products as a separate "
        "licensed dissemination channel (not part of the API Platform). This page does "
        "not probe TRAQS or trade-reporting endpoints."
    ),
    units_note="Not available through the Query API credentials used here.",
    query_api=False,
)

QUERY_DATASETS: tuple[FinraDatasetSpec, ...] = (
    CORPORATE_BREADTH,
    CORPORATE_SENTIMENT,
    CORPORATE_CAPPED_VOLUME,
)

ALL_DATASETS: tuple[FinraDatasetSpec, ...] = QUERY_DATASETS + (TRACE_INDIVIDUAL,)
QUERY_DATASETS_BY_NAME = {spec.dataset: spec for spec in QUERY_DATASETS}
ALL_DATASETS_BY_NAME = {spec.dataset: spec for spec in ALL_DATASETS}

BREADTH_DISPLAY_CATEGORIES = (
    "all securities",
    "investment grade",
    "high yield",
    "convertibles",
)
SENTIMENT_CUSTOMER_BUY = "customer buy"
SENTIMENT_CUSTOMER_SELL = "customer sell"


def category_key(spec: FinraDatasetSpec, row: dict) -> str:
    parts = []
    for field_name in spec.grain_fields:
        value = row.get(field_name)
        parts.append("" if value is None else str(value).strip())
    return "|".join(parts) or "default"
