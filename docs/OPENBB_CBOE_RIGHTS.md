# OpenBB / Cboe rights model

Reviewed 2026-09-14. Canonical operator record for OPTIONS vs VIX gates.
Project governance is **not** a Cboe licence. Do not set product rights flags from this file.

## Products (independent)

| Source id | Provider | Actual product | Endpoint used by OpenBB | Rights flag | Enable flag | Default Data Health |
|---|---|---|---|---|---|---|
| `OPENBB_CBOE_OPTIONS` | OpenBB / Cboe website delayed quotes | Delayed options chains JSON backing [cboe.com/delayed_quotes](https://www.cboe.com/delayed_quotes/) | `https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json` (indexes `_{symbol}.json`) | `MI_OPENBB_OPTIONS_RIGHTS_ACK` | `MI_OPENBB_OPTIONS_ENABLED` | `RIGHTS_PENDING` |
| `OPENBB_CBOE_VIX` | OpenBB / Cboe CFE delayed quotes | VX_EOD 4 p.m. ET futures levels (not official settlement) | Same delayed-quotes path via CFE-style symbols (`obb.derivatives.futures.curve(symbol="VX_EOD", provider="cboe")`) | `MI_OPENBB_VIX_RIGHTS_ACK` | `MI_OPENBB_VIX_ENABLED` | `AGREEMENT_REQUIRED` |

`MI_OPENBB_CBOE_RIGHTS_ACK` is a **legacy umbrella**. It is ignored. It does not authorize either product.

Independent axes: source enabled, entitlement/rights, collection, export scope, Data Health. OPTIONS consent does not imply VIX authorization.

## OPTIONS — licensing evidence (2026-09-14)

Classification: unauthenticated Cboe **website** delayed-quotes JSON. Not a documented licensed MDP / All Access API product.

Cboe Website Terms ([https://www.cboe.com/en/terms/](https://www.cboe.com/en/terms/), last updated **16 Nov 2022**, retrieved 2026-09-14):

- Allowed: view, print, and download **one copy** for personal non-commercial use.
- Prohibited without prior written consent: store in an electronic retrieval system; transmit; redistribute; create derivative works (examples include a financial product, service, or index).

Consent path ([https://www.cboe.com/en/use-of-content/](https://www.cboe.com/en/use-of-content/), retrieved 2026-09-14):

1. Email **permissions@cboe.com** with the six requested items.
2. Cboe typically responds in five business days (no obligation).
3. If Cboe approves, approval is **contingent on executing a license agreement**. Use is not approved until both parties sign.
4. Submitting a request does not grant permission.

North American Data Policies (Cboe Global Markets, [Market_Data_Policies_Redlined.pdf](https://cdn.cboe.com/resources/membership/Market_Data_Policies_Redlined.pdf), retrieved 2026-09-14) govern **licensed Data** under the Global Data Agreement. Non-Display Usage is machine/automated access not solely in support of a display. Those policies do **not** reclassify the website JSON as a free licensed delayed-options feed.

Licensed Cboe Options Top internal distribution is published at **$9,000/month** on the Cboe Exchange Fees Schedule effective 1 Sep 2026 ([Cboe_FeeSchedule.pdf](https://cdn.cboe.com/resources/membership/Cboe_FeeSchedule.pdf)). That is a different product from the website JSON.

**Activation status:** BLOCKED. Do not set `MI_OPENBB_OPTIONS_RIGHTS_ACK=1`, `MI_OPENBB_OPTIONS_ENABLED=1`, or `MI_OPENBB_INSTALL_EXTRA=1` until Cboe written consent plus any required licence exist.

Intended conservative cadence **if later authorized:** one weekday snapshot after 16:05 America/New_York. Do not enable `MI_OPENBB_INTRADAY_SNAPSHOTS`.

## VIX / CFE — licensing evidence (2026-09-14)

Cboe Market Data Document Library ([document library](https://www.cboe.com/market_data_services/document_library/), retrieved 2026-09-14): Step 1 sign the **Cboe Global Data Agreement**; Step 2 request data through the Onboarding Portal.

North American Data Policies table: **CFE Data Agreement Required** (delayed / EOD / historical subscriber column in the retrieved PDF). Do not treat OPTIONS website consent as covering CFE.

**Activation status:** BLOCKED. Adapter preserved. Collection off. Data Health `AGREEMENT_REQUIRED`, not `FAILED`.

Cheapest compliant **non-curve** alternative: FRED `VIXCLS` is the public **spot VIX index**, not a VX futures term structure, and it is not currently in this project's FRED catalog. Adding it would be a separate FRED series decision. IBKR VX only if an existing TWS entitlement already covers it. Do not buy a CFE feed from this review.

## Export / AI / MCP

Export scope: `INTERNAL_ONLY`. Not on `AI_GATEWAY_REMOTE_VALUE_SOURCES`. Remote sessions stay fail-closed for raw chains, strikes, IV, quotes, and VIX curve values. Derived dashboard calculations remain internal unless a later entitlement explicitly allows remote values.

## Disable procedure

Set all of `MI_OPENBB_OPTIONS_ENABLED`, `MI_OPENBB_VIX_ENABLED`, `MI_OPENBB_OPTIONS_RIGHTS_ACK`, `MI_OPENBB_VIX_RIGHTS_ACK`, `MI_OPENBB_INSTALL_EXTRA` to `0`. Leave stored snapshots; Streamlit is read-only.

## Data Health

| Access | Policy | Meaning |
|---|---|---|
| `ENTITLEMENT_REQUIRED` | `RIGHTS_PENDING` | OPTIONS: Cboe website consent not recorded |
| `AGREEMENT_REQUIRED` | `AGREEMENT_REQUIRED` | VIX/CFE: Data Agreement not recorded |
| `DISABLED` | `DISABLED` | Product rights recorded; collection off |
| `CONFIGURATION_REQUIRED` | (technical) | OpenBB extra not installed |
| `CONFIGURED` | (technical) | Rights + enable + extra present |

A licensing gate is not `FAILED`.
